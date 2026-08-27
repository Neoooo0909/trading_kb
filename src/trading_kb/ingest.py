"""研报重 lane 摄入编排(§9 摄入管线)。

read report_lab cards → 分流 → 双轨成色 → 实体归一 → 入图(去重合并)。
hard_fact/quant_fact/view → facts_store;structure → structure_store;
background → facts_store.background_log 留痕(v3,2026-08-26;此前是直接 return 无痕丢弃,
"留原文"只写在注释里从未实现,六到七成 findings 消失且不可审计,见 docs/BACKGROUND_FIX_PLAN_20260826.md)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config
from .classify import classify_with_reason, has_real_entity, predicate_for, relation_for
from .critique import CritiqueEngine
from .entity_quality import attribute_subject, is_garbage_entity, is_ib_firm, is_pseudo_company
from .entity_registry import EntityRegistry, UNKNOWN_CID
from .facts_store import FactsStore
from .grade import grade_fact
from .models import Fact, Finding, LEVEL_RANK, Relation, _normalize, content_grams, level_down
from .report_lab_adapter import card_entities, card_to_findings, iter_cards
from .structure_store import StructureStore
from .verify_hooks import make_verifier
from .web_enrich import make_announcement_verifier, make_corroborator

# 订单事实强度递进(用于自动 supersede:弱→强进展替代旧事实,§10.3)
_ORDER_PROGRESSION = {
    "HAS_ORDER_RUMOR": 1, "HAS_ORDER_INTENT": 2,
    "HAS_CONFIRMED_ORDER": 3, "HAS_DELIVERY_VALIDATION": 4,
}
# 反证/澄清类 predicate(触发 disputed)。
# HAS_CLARIFICATION 是公告 lane(announcements_to_kb)的澄清公告谓词,2026-08-06 补进来
# 保持口径一致;公告 lane 不经 ResearchIngestor,其 disputed 标记在该脚本内自行处理。
_CONTRADICTING = {"HAS_CLARIFICATION_RISK", "HAS_CLARIFICATION", "CONTRADICTS", "HAS_DEMAND_RISK"}


@dataclass
class IngestReport:
    """单次摄入回执(§9 [10])。"""
    cards: int = 0
    findings: int = 0
    hard_facts: int = 0
    quant_facts: int = 0
    structures: int = 0
    views: int = 0            # v3:有主体定性论断(入库、可检索、不做结论)
    background: int = 0       # 无主体无数字 → background_log 留痕(不入 facts)
    dup_skipped: int = 0      # (doc, claim) 判重拦下的再入库(2026-08-27)
    entities_registered: int = 0
    level_dist: dict = None
    doubts: int = 0           # 带质疑标记的事实数
    doubt_high: int = 0       # 高严重度质疑数

    def __post_init__(self):
        if self.level_dist is None:
            self.level_dist = {lv: 0 for lv in LEVEL_RANK}   # 档位从唯一定义点派生


class ResearchIngestor:
    """研报重 lane 管线。"""

    def __init__(self, registry: EntityRegistry, facts: FactsStore,
                 structure: StructureStore, verify=None, llm_classify=None,
                 critique_engine: CritiqueEngine | None = None):
        self.registry = registry
        self.facts = facts
        self.structure = structure
        # 验证器:开启联网时优先用权威公告验证,否则用本地数据验证钩子
        self.verify = verify if verify is not None else (
            make_announcement_verifier() or make_verifier())
        self.llm_classify = llm_classify
        self.critique = critique_engine                 # 质疑引擎(已 fit),None 则不体检
        self.corroborator = make_corroborator()         # 权威佐证钩子(联网开启时生效)

    def ingest_finding(self, f: Finding, report: IngestReport,
                       code_map: dict | None = None,
                       card_entity_names: list[str] | None = None,
                       card: dict | None = None) -> None:
        """单条 finding 全流程:分流 → 成色 → 归一 → 入图。

        code_map: 卡片级 实体名→证券代码,用于把硬事实主语锚到真实代码(N6)。
        card_entity_names: 卡片级实体名,供结构关系补全第二端(C3)。
        card: 源卡片,供主体归属(点名匹配 / title 锚定主导主体,治未知主体)。
        """
        cat, reason = classify_with_reason(f, llm=self.llm_classify)

        if cat == "background":
            # v3:留痕而非无痕丢弃——"不该要"与"没判对"必须事后可区分(同 _util.read_jsonl
            # 静默吞行的教训)。不进 FTS/向量,不参与检索。reason 分档(boilerplate /
            # llm_override / no_entity_no_number),此前一律硬编码成最后一种,审计字段失真。
            report.background += 1
            self.facts.log_background(f.doc_id, f.claim, f.entities, f.source_date,
                                      f.source_kind, reason=reason or "no_entity_no_number")
            return

        if cat == "structure":
            self._ingest_structure(f, report, card_entity_names or [],
                                   code_map=code_map or {}, card=card)
            return

        # hard_fact / quant_fact / view → facts_store
        self._ingest_fact(f, cat, report, code_map or {}, card=card)

    def _ingest_fact(self, f: Finding, cat: str, report: IngestReport,
                     code_map: dict, card: dict | None = None) -> None:
        """硬事实/量化事实/定性论断(view)入时序事实层。"""
        if cat == "quant_fact":
            predicate = "HAS_FACTOR_PERFORMANCE"
        elif cat == "view":
            predicate = "HAS_VIEW"        # 不在 _ORDER_PROGRESSION/VERIFIABLE/_CONTRADICTING 内
        else:
            predicate = predicate_for(f)

        level, unver = grade_fact(f, predicate, verify=self.verify)
        if cat == "view":
            # 定性论断可靠性低于同源硬事实:信源基线降一档(broker B→C、social C→D),
            # 恒 unverifiable。C/D 会被 ask._low_grade_views 收进"情绪面·不同观点"段。
            level, unver = level_down(level), True

        # 主语取首个实体,优先用卡片级 code 锚定真实证券代码(N6)
        # A3:不再因 cat==hard_fact 就强制 stock(避免"上交所/监管机构"被错挂股票);
        #     只有拿到股票 code 才按股票归一,否则按概念。
        subject = _pick_subject(f, card or {})
        code = code_map.get(_normalize(subject))
        etype = "stock" if code else "concept"
        cid = self.registry.resolve(subject, type_=etype, stock_code=code)

        # (doc, claim) 判重预检(与 facts_store.upsert 同一判定点 find_doc_claim_dup):同文同句已有行
        # 就不做质疑体检——此前体检在 upsert 之前,dup_skipped 的 finding 仍累加 report.doubts,
        # 且可能白跑一次联网佐证。预检与 upsert 因并发插入不一致时以 upsert 为准(无害)。
        if self.facts.find_doc_claim_dup([f.doc_id], f.claim, cid) is not None:
            self.facts.last_upsert_dup = True
            report.dup_skipped += 1
            ents = [e for e in (f.entities or []) if isinstance(e, str) and e.strip()]
            same = self.facts.find_doc_claim_dup([f.doc_id], f.claim, cid)
            if ents and same["status"] in ("active", "disputed"):
                self.facts.set_extra_entities(same["fact_id"], ents)
            return

        # 质疑体检:产出存疑标记,随事实落库(供六段式展示)
        doubts = []
        max_sev = None
        if self.critique is not None:
            cres = self.critique.critique(f, web=self.corroborator)
            doubts = [{"kind": fl.kind, "severity": fl.severity, "message": fl.message}
                      for fl in cres.flags]
            max_sev = cres.max_severity
            if doubts:
                report.doubts += 1
                if max_sev == "high":
                    report.doubt_high += 1

        fact = Fact(
            subject=subject, predicate=predicate,
            object=f.claim[:80], canonical_id=cid, claim=f.claim,
            evidence_level=level, unverifiable=unver, source_kind=f.source_kind,
            sources=[f.doc_id], valid_at=f.source_date, category=cat,
            extra={"evidence": f.evidence[:200], "page": f.page,
                   "verified_numbers": f.verified_numbers, "broker": f.broker,
                   "doubts": doubts, "doubt_severity": max_sev,
                   # P1(2026-08-26):完整实体列表随事实落库并进 FTS——主体只取首实体,
                   # 其余实体此前全丢(抽样 46% 的 finding 有 ≥2 实体)。不放大 fact 数、不动主体归属。
                   "entities": [e for e in (f.entities or [])
                                if isinstance(e, str) and e.strip()],
                   "rule_version": "v3"},
        )
        new_id = self.facts.upsert(fact)
        if self.facts.last_upsert_dup:          # 同文同句已有行:不计新事实、不做冲突消解/质疑
            report.dup_skipped += 1
            return

        # 自动状态机:硬事实做冲突检测(进展替代 / 矛盾置争议),§10.3 四类结局
        if cat == "hard_fact":
            self._resolve_conflicts(fact, new_id)
            report.hard_facts += 1
        elif cat == "view":
            report.views += 1
        else:
            report.quant_facts += 1
        report.level_dist[level] = report.level_dist.get(level, 0) + 1

    def _resolve_conflicts(self, new_fact: Fact, new_id: str) -> None:
        """新硬事实与同主体已有 active 事实的冲突消解(§10.3 自动状态机)。

        - 订单进展(传闻→意向→确认→交付):新更强 + 不更旧 → supersede 旧。
        - 矛盾/澄清类:对同主体既有事实标 disputed。
        默认保守:仅在主体(canonical_id)相同且对象有 token 重叠时动作,避免误伤。

        性能(2026-08-26):下方两种动作都要求新事实谓词 ∈ 订单进展族 或 反证族,其他谓词
        (HAS_CATALYST/FORECAST/FINANCIAL_METRIC/RATING/VIEW,占新事实绝大多数)进来只会白读——
        而 query 按 canonical_id 拉 200 条要先把该主体全部行读出来排序,"未知主体"10.7 万行一次 5.1s、
        英伟达 1.3 万行 1.8s,回填 research lane 因此 8 分钟走不完 500 张卡。先按谓词早退,语义不变。
        """
        new_strength = _ORDER_PROGRESSION.get(new_fact.predicate)
        if new_strength is None and new_fact.predicate not in _CONTRADICTING:
            return
        existing = self.facts.query(canonical_id=new_fact.canonical_id,
                                    include_invalidated=False, limit=200)
        for e in existing:
            if e["fact_id"] == new_id:
                continue
            if not _object_overlap(e["object"], new_fact.object):
                continue
            old_strength = _ORDER_PROGRESSION.get(e["predicate"])
            # 进展替代:两者都在订单族,新更强,且新不早于旧
            if (new_strength and old_strength and new_strength > old_strength
                    and (new_fact.valid_at or "") >= (e["valid_at"] or "")):
                self.facts.supersede(e["fact_id"], new_fact, at=new_fact.valid_at or "")
            # 矛盾置争议
            elif new_fact.predicate in _CONTRADICTING and old_strength:
                self.facts.mark_disputed(e["fact_id"])

    def _ingest_structure(self, f: Finding, report: IngestReport,
                          card_entity_names: list[str],
                          code_map: dict | None = None, card: dict | None = None) -> None:
        """结构关系入结构层。两端实体:优先 finding 内,不足时从卡片级实体补(C3)。
        不足两端 / 两端同一实体时不强造边——v3 起改走 view 入 facts(有主体即可检索),
        此前是计入 background 后丢弃;仍无任何实体的才留痕。"""
        rel_type = relation_for(f) or "BELONGS_TO_SEGMENT"
        ents = list(dict.fromkeys(f.entities))   # 去重保序
        if len(ents) < 2:
            # 从卡片级实体里找在 claim 文本中出现的、与 finding 实体不同的补第二端
            claim = f.claim
            for name in card_entity_names:
                if name and name in claim and name not in ents:
                    ents.append(name)
                if len(ents) >= 2:
                    break
        # 垃圾端点不建边(2026-08-27):"海外市场""美伊停火""上游"这类被 is_garbage_entity 拦在实体表
        # 之外的名字,此前仍会 resolve 出 concept:/company: id 挂成边 → 135 条端点不存在的孤儿边。
        # 剔掉后不足两端走 view/留痕,与"不强造边"的口径一致。
        garbage = [e for e in ents if is_garbage_entity(e, "concept")]
        if garbage:
            ents = [e for e in ents if e not in garbage]
        if len(ents) < 2:
            self._structure_fallback(f, report, code_map, card,
                                     "structure_garbage_end" if garbage else "structure_lt2")
            return
        src = self.registry.resolve(ents[0])
        dst = self.registry.resolve(ents[1])
        if src == dst:
            self._structure_fallback(f, report, code_map, card, "structure_same_ends")
            return
        if UNKNOWN_CID in (src, dst):
            # 端点被注册表判成垃圾(归到未知主体):不建"未知主体"hub 边(生产曾累积 464 条,
            # neighbors() 会把"未知主体"当邻居)。
            self._structure_fallback(f, report, code_map, card, "structure_unknown_end")
            return
        self.structure.upsert(Relation(
            src=src, rel_type=rel_type, dst=dst, sources=[f.doc_id],
        ))
        report.structures += 1

    def _structure_fallback(self, f: Finding, report: IngestReport, code_map, card,
                            reason: str) -> None:
        """结构边造不出来时的去向:有**非垃圾**实体 → view 入 facts;否则 → background_log。

        判"有实体"必须与 classify._has_real_entity 同口径(审核 F8):此前用"任意非空串",
        仅垃圾端("海外市场")的 structure 句会落成 subject=未知主体 的 view(生产 171 行)。"""
        if has_real_entity(f):
            self._ingest_fact(f, "view", report, code_map or {}, card=card)
        else:
            report.background += 1
            self.facts.log_background(f.doc_id, f.claim, f.entities, f.source_date,
                                      f.source_kind, reason=reason)

    def ingest_card(self, card: dict, report: IngestReport) -> None:
        """摄入一张卡片:先登记卡片级实体(含 code),再摄入 findings。"""
        code_map: dict[str, str] = {}
        card_entity_names: list[str] = []
        for e in card_entities(card):
            name = e.get("name")
            if not isinstance(name, str) or not name.strip():
                continue                       # 实体名缺失/非字符串(LLM 偶发畸形) → 跳这一个,不毁整卡
            kind = e.get("kind") or "concept"
            etype = _kind_to_type(kind)        # _kind_to_type 已对非字符串 kind 容错
            # 闸门(P2-F①,2026-08-25):LLM 把"AI需求/北美数据中心/HBM先进封装"这类主题短语标成
            # company → 降为 concept 登记。否则 company: 前缀被 ask 当证券,劫持查询进快路径。
            if etype == "company" and is_pseudo_company(name):
                etype = "concept"
            self.registry.resolve(name, type_=etype, stock_code=e.get("code"))
            card_entity_names.append(name)
            # A4:只有"股票"类才进 code_map(基金/指数/产品不锚成股票)
            if e.get("code") and kind == "stock":
                code_map[_normalize(name)] = e["code"]
            report.entities_registered += 1
        for f in card_to_findings(card):
            report.findings += 1
            self.ingest_finding(f, report, code_map=code_map,
                                card_entity_names=card_entity_names, card=card)
        report.cards += 1


def _pick_subject(f: Finding, card: dict) -> str:
    """选论断主语(治本·多病同治,精度优先)。

    ① 跳过垃圾实体**与作者投行**取首个真实体——治"垃圾实体当主语"。
    ② 无可用实体 → attribute_subject(免责剔除 / 点名匹配 / title 锚定主导主体,且关系/指代论断
       不走点名匹配以免方向性挂反)——治未知主体主力。③ 再退 broker;④ 最后未知主体。

    ①的 is_ib_firm 闸是 2026-08-03 加的:此前研报卡的 findings 根本不带 entities(prompt 没要),
    这一级恒空、形同虚设;补上 entities 后它才真正生效,而研报里最密集的具名机构恰恰是**出报告的
    投行自己**(Morgan Stanley/中金/高盛/UBS…),不拦就会把作者当主体——误归属比未知主体更糟。
    用 is_ib_firm 而非另造名单,是为了与②的 card_subject_entities 同口径(它也用这个)。
    已知其子串匹配会误伤同名非投行(中金黄金/广发银行),但那是既有行为:②同样会剔掉它们,
    故此处误伤不产生回归(结果仍是未知主体),只是少赚一点。
    """
    for e in (f.entities or []):
        if (isinstance(e, str) and e.strip()
                and not is_garbage_entity(e, "concept") and not is_ib_firm(e)):
            return e
    named = attribute_subject(f"{f.claim} {getattr(f, 'evidence', '')}", card or {})
    return named or f.broker or "未知主体"


def _kind_to_type(kind: str) -> str:
    """report_lab 实体 kind → 注册表 type(A3/A4 归一)。kind 偶被 LLM 抽成 list/None,容错为 concept。"""
    k = (kind if isinstance(kind, str) else "").lower()
    if k == "stock":
        return "stock"
    if k == "fund":
        return "fund"
    if k == "product":
        return "product"
    if k == "index":
        return "index"
    if k in ("company", "person", "material"):
        return k
    return "concept"


def _object_overlap(a: str, b: str) -> bool:
    """两个 object 是否指向同一事(共享内容)。

    无 jieba 环境:中文用字符 2-gram 重叠,英数用词;保守判定避免误伤无关事实。
    """
    return bool(content_grams(a) & content_grams(b))


def run_ingest(limit: Optional[int] = None, llm_classify=None) -> IngestReport:
    """端到端摄入入口:读 report_lab 全部卡片 → 入三层。返回回执。

    llm_classify:可选 LLM 分类钩子(签名 (Finding)->Category)。默认 None 走规则核心。
    config.USE_LLM=1 时自动接 Kimi→DeepSeek→Sonnet 分类器(A 分流);显式传入则覆盖。
    """
    if llm_classify is None and config.USE_LLM:        # A：USE_LLM 自动启用 LLM 分流
        from .llm import make_llm_classify
        llm_classify = make_llm_classify()
    config.ensure_data_dir()
    registry = EntityRegistry(config.ENTITY_DB)
    facts = FactsStore(config.FACTS_DB)
    structure = StructureStore(config.STRUCTURE_DB)
    try:
        # 第一遍:收集全部 findings,拟合质疑引擎(② 乐观判定需全库分位基准)
        cards = list(iter_cards())
        if limit:
            cards = cards[:limit]
        all_findings = []
        for card in cards:
            all_findings.extend(card_to_findings(card))
        critique_engine = CritiqueEngine().fit(all_findings)

        # 第二遍:摄入并逐条体检
        ingestor = ResearchIngestor(registry, facts, structure,
                                    llm_classify=llm_classify, critique_engine=critique_engine)
        report = IngestReport()
        for card in cards:
            ingestor.ingest_card(card, report)
        return report
    finally:            # 中途异常也不漏连接(web 长驻进程反复调用,靠 GC 不可靠)
        registry.close()
        facts.close()
        structure.close()

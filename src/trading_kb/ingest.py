"""研报重 lane 摄入编排(§9 摄入管线)。

read report_lab cards → 分流 → 双轨成色 → 实体归一 → 入图(去重合并)。
hard_fact/quant_fact → facts_store;structure → structure_store;background → 跳过(留原文)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config
from .classify import classify_finding, predicate_for, relation_for
from .critique import CritiqueEngine
from .entity_registry import EntityRegistry
from .facts_store import FactsStore
from .grade import grade_fact
from .models import Fact, Finding, Relation, _normalize, content_grams
from .report_lab_adapter import card_entities, card_to_findings, iter_cards
from .structure_store import StructureStore
from .verify_hooks import make_verifier
from .web_enrich import make_announcement_verifier, make_corroborator

# 订单事实强度递进(用于自动 supersede:弱→强进展替代旧事实,§10.3)
_ORDER_PROGRESSION = {
    "HAS_ORDER_RUMOR": 1, "HAS_ORDER_INTENT": 2,
    "HAS_CONFIRMED_ORDER": 3, "HAS_DELIVERY_VALIDATION": 4,
}
# 反证/澄清类 predicate(触发 disputed)
_CONTRADICTING = {"HAS_CLARIFICATION_RISK", "CONTRADICTS", "HAS_DEMAND_RISK"}


@dataclass
class IngestReport:
    """单次摄入回执(§9 [10])。"""
    cards: int = 0
    findings: int = 0
    hard_facts: int = 0
    quant_facts: int = 0
    structures: int = 0
    background: int = 0
    entities_registered: int = 0
    level_dist: dict = None
    doubts: int = 0           # 带质疑标记的事实数
    doubt_high: int = 0       # 高严重度质疑数

    def __post_init__(self):
        if self.level_dist is None:
            self.level_dist = {"A": 0, "B+": 0, "B": 0, "C": 0, "D": 0}


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
                       card_entity_names: list[str] | None = None) -> None:
        """单条 finding 全流程:分流 → 成色 → 归一 → 入图。

        code_map: 卡片级 实体名→证券代码,用于把硬事实主语锚到真实代码(N6)。
        card_entity_names: 卡片级实体名,供结构关系补全第二端(C3)。
        """
        cat = classify_finding(f, llm=self.llm_classify)

        if cat == "background":
            report.background += 1
            return

        if cat == "structure":
            self._ingest_structure(f, report, card_entity_names or [])
            return

        # hard_fact / quant_fact → facts_store
        self._ingest_fact(f, cat, report, code_map or {})

    def _ingest_fact(self, f: Finding, cat: str, report: IngestReport,
                     code_map: dict) -> None:
        """硬事实/量化事实入时序事实层。"""
        if cat == "quant_fact":
            predicate = "HAS_FACTOR_PERFORMANCE"
        else:
            predicate = predicate_for(f)

        level, unver = grade_fact(f, predicate, verify=self.verify)

        # 主语取首个实体,优先用卡片级 code 锚定真实证券代码(N6)
        # A3:不再因 cat==hard_fact 就强制 stock(避免"上交所/监管机构"被错挂股票);
        #     只有拿到股票 code 才按股票归一,否则按概念。
        subject = f.entities[0] if f.entities else (f.broker or "未知主体")
        code = code_map.get(_normalize(subject))
        etype = "stock" if code else "concept"
        cid = self.registry.resolve(subject, type_=etype, stock_code=code)

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
                   "doubts": doubts, "doubt_severity": max_sev},
        )
        new_id = self.facts.upsert(fact)

        # 自动状态机:硬事实做冲突检测(进展替代 / 矛盾置争议),§10.3 四类结局
        if cat == "hard_fact":
            self._resolve_conflicts(fact, new_id)
            report.hard_facts += 1
        else:
            report.quant_facts += 1
        report.level_dist[level] = report.level_dist.get(level, 0) + 1

    def _resolve_conflicts(self, new_fact: Fact, new_id: str) -> None:
        """新硬事实与同主体已有 active 事实的冲突消解(§10.3 自动状态机)。

        - 订单进展(传闻→意向→确认→交付):新更强 + 不更旧 → supersede 旧。
        - 矛盾/澄清类:对同主体既有事实标 disputed。
        默认保守:仅在主体(canonical_id)相同且对象有 token 重叠时动作,避免误伤。
        """
        existing = self.facts.query(canonical_id=new_fact.canonical_id,
                                    include_invalidated=False, limit=200)
        new_strength = _ORDER_PROGRESSION.get(new_fact.predicate)
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
                          card_entity_names: list[str]) -> None:
        """结构关系入结构层。两端实体:优先 finding 内,不足时从卡片级实体补(C3)。"""
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
        if len(ents) < 2:
            report.background += 1   # 仍不足两端,不强造边
            return
        src = self.registry.resolve(ents[0])
        dst = self.registry.resolve(ents[1])
        if src == dst:
            report.background += 1
            return
        self.structure.upsert(Relation(
            src=src, rel_type=rel_type, dst=dst, sources=[f.doc_id],
        ))
        report.structures += 1

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
            self.registry.resolve(name, type_=etype, stock_code=e.get("code"))
            card_entity_names.append(name)
            # A4:只有"股票"类才进 code_map(基金/指数/产品不锚成股票)
            if e.get("code") and kind == "stock":
                code_map[_normalize(name)] = e["code"]
            report.entities_registered += 1
        for f in card_to_findings(card):
            report.findings += 1
            self.ingest_finding(f, report, code_map=code_map,
                                card_entity_names=card_entity_names)
        report.cards += 1


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
    registry.close()
    facts.close()
    structure.close()
    return report

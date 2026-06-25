"""六段式检索问答(§11)。

编排:结构定位(structure)→ 状态查成色(facts)→ 证据回溯(report_lab text/cards)。
默认产出"材料包 + 结构化六段骨架"(给 Claude 会话合成);USE_LLM=1 可直接作答。

六段:结论 / 证据链 / 分歧反证 / 后续验证 / 交易含义 / 引用来源。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .entity_registry import EntityRegistry
from .facts_store import FactsStore, _LEVEL_RANK
from .structure_store import StructureStore
from .models import _normalize, content_grams as _content_grams


@dataclass
class AskResult:
    """六段式检索结果(材料包)。"""
    query: str
    canonical_id: str = ""
    facts: list[dict] = field(default_factory=list)
    invalidated_facts: list[dict] = field(default_factory=list)
    neighbors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_six_section(self) -> str:
        """渲染六段式骨架(证据不足时显式提示)。"""
        lines = [f"# 问:{self.query}", ""]
        if not self.facts and not self.neighbors:
            lines.append("**证据不足**:知识库中未找到与该查询匹配的事实或结构关系。")
            lines.append("建议:先入相关研报,或换更具体的实体/主线词。")
            return "\n".join(lines)

        # 结论(N1:排除 disputed,争议项只进"分歧/反证"段)
        lines.append("## 结论")
        active = [f for f in self.facts if f["status"] == "active"]
        if active:
            top = active[0]
            tag = _grade_tag(top)
            lines.append(f"{top['claim']} {tag}")
        else:
            lines.append("(无 active 事实,见下方分歧/反证)")

        # 证据链(带成色编号 + 质疑图标)
        lines.append("\n## 证据链")
        for i, f in enumerate(active[:8], 1):
            dmark = _doubt_icon(f)
            lines.append(f"[F{i}] {_grade_tag(f)}{dmark} {f['claim']}  "
                         f"(来源{f['support_count']}篇, 数字校验{_vn(f)})")

        # 质疑提示(批判性体检:无出处/过于乐观/回测软肋)
        doubt_items = [(i, f) for i, f in enumerate(active[:8], 1) if _doubts(f)]
        if doubt_items:
            lines.append("\n## ⚠ 质疑提示")
            lines.append("（自动批判性体检，提醒别全信；不代表结论一定错）")
            for i, f in doubt_items:
                for d in _doubts(f):
                    sev = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(d.get("severity"), "•")
                    lines.append(f"- {sev} [F{i}] {d.get('message','')}")

        # 分歧/反证
        lines.append("\n## 分歧/反证")
        disputed = [f for f in self.facts if f["status"] == "disputed"]
        if disputed or self.invalidated_facts:
            for f in disputed:
                lines.append(f"[争议] {f['claim']}")
            for f in self.invalidated_facts[:5]:
                lines.append(f"[已证伪/替代·{f['status']}] {f['claim']}")
        else:
            lines.append("(暂无已记录的反证;不代表无风险)")

        # 后续验证
        lines.append("\n## 后续验证")
        unver = [f for f in active if f["unverifiable"]]
        if unver:
            lines.append(f"以下 {len(unver)} 条为待验证(unverifiable),需盯公告/数据印证:")
            for f in unver[:5]:
                lines.append(f"- {f['claim'][:60]}")
        else:
            lines.append("(主要事实已有信源基线;高价值结论建议仍交叉验证)")

        # 交易含义
        lines.append("\n## 交易含义")
        lines.append("(由上层 LLM 结合成色与反证综合;低成色/待验证项不应作为独立买点)")

        # 引用来源
        lines.append("\n## 引用来源")
        srcs = sorted({s for f in active for s in _sources(f)})
        for s in srcs[:12]:
            lines.append(f"- report_lab card: {s}")

        if self.warnings:
            lines.append("\n## ⚠ 检索告警")
            lines.extend(f"- {w}" for w in self.warnings)
        return "\n".join(lines)


class AskEngine:
    """六段式检索引擎。"""

    def __init__(self, registry: EntityRegistry, facts: FactsStore, structure: StructureStore):
        self.registry = registry
        self.facts = facts
        self.structure = structure

    def ask(self, query: str, include_invalidated: bool = False,
            use_semantic=None) -> AskResult:
        """检索:实体定位 + 文本/语义召回 → 统一(相关度×成色×时效)加权排序(P0)。

        候选池 = 实体命中事实 ∪ facts.search(SQL LIKE 预筛，根治旧版只扫前 2000 的召回坍缩)
                 ∪(可选)语义召回(本地 embedding，P0-b)。
        语义召回较重(加载模型 + 全库向量，约数秒)，故默认仅在"实体未命中"时启用——
        带实体名的查询走实体命中快路径已精准。use_semantic=True/False 显式覆盖。
        加权排序治两病:① 召回坍缩;② 老高成色研报永远压新社媒(时效项让最新内容也能上榜)。
        """
        result = AskResult(query=query)
        cid = self._locate_entity(query)
        result.canonical_id = cid or ""
        # 语义召回触发:无实体 **或** 定位到的是非证券概念(发现型/topic 查询)。治"存储测试设备龙头"
        # 锚到 concept 后跳过语义、池里只剩字面"存储"的募资公告、高语义的龙头股(精智达)进不来。
        # 个股/公司命中走精准快路径(其自有事实已准),不跑较重的语义召回。
        want_sem = (cid is None or not _is_security(cid)) if use_semantic is None else bool(use_semantic)

        # 候选池:实体命中 ∪ LIKE 召回 ∪(可选)语义召回
        pool: list[dict] = []
        if cid:
            pool += self.facts.query(canonical_id=cid, include_invalidated=False, limit=120)
            result.neighbors = self.structure.neighbors(cid)
        pool = _merge_facts(pool, self.facts.search(query, limit=400))
        if want_sem:
            pool = _merge_facts(pool, self._semantic_recall(query, top_k=120))

        result.facts = self._rank_facts(query, pool, cid, use_semantic=want_sem)

        if cid and include_invalidated:
            allf = self.facts.query(canonical_id=cid, include_invalidated=True, limit=120)
            result.invalidated_facts = [f for f in allf
                                        if f["status"] in ("superseded", "invalidated", "expired")]
        if not cid and result.facts:
            result.warnings.append("未定位到具体实体,已用关键词+语义加权检索")
        elif not cid:
            result.warnings.append("未定位到具体实体,且关键词/语义无命中")
        if not result.facts and not result.neighbors:
            result.warnings.append("证据不足:无匹配事实/关系")
        return result

    def _rank_facts(self, query: str, facts: list[dict], cid,
                    use_semantic: bool = False) -> list[dict]:
        """综合加权排序(P0):相关度为主，成色×时效×来源数为辅。

        relevance = 字面覆盖率 + 实体精确命中(强信号) + 语义相似(P0-b/P0.5 注入)，三项同量纲。
        relevance<=0 丢弃(无关);其余按 score 降序。
        score = relevance×2 + 成色(0~1) + 时效(0~1) + 来源数(0~0.5)。

        字面项归一为"覆盖率"(命中 gram / query gram 数，0~1)而非裸重叠数：裸数无上界，
        长 claim 命中十几个 gram 会把语义满分(≤2)和实体命中(=2)压到后面，使 P0.5 语义召回
        捞进来的、字面不同但语义相关的事实在排序阶段被沉底，语义价值被抵消。
        """
        from datetime import date as _Date
        qg = _content_grams(query)
        nq = max(len(qg), 1)
        today = _Date.today().toordinal()
        sem = self._semantic_scores(query, facts) if use_semantic else {}   # 仅按需调语义
        scored = []
        for f in facts:
            fg = _content_grams(f"{f.get('claim','')} {f.get('object','')}")
            rel = len(qg & fg) / nq                         # 字面覆盖率 0~1(归一防长 claim 压垮语义)
            ent_hit = 1.0 if (cid and f.get("canonical_id") == cid) else 0.0
            sscore = sem.get(f.get("fact_id"), 0.0)
            relevance = 1.5 * rel + 2.0 * ent_hit + 2.0 * sscore   # 字面/实体/语义同量纲可比
            if relevance <= 0:
                continue
            level = _LEVEL_RANK.get(f.get("evidence_level"), 1) / 4.0
            rec = _recency(f.get("valid_at"), today)
            sup = min(f.get("support_count") or 0, 10) / 10.0
            scored.append((relevance * 2.0 + level + rec + sup * 0.5, f))
        scored.sort(key=lambda x: -x[0])
        return _diversify_by_kind(scored)

    def _semantic_index(self):
        """取共享本地语义索引(P0-b)；未装 embedding / 无索引时返回 None(优雅降级)。"""
        try:
            from .semantic import SemanticIndex
            return SemanticIndex.shared(self.facts.db_path)
        except Exception:
            return None

    def _semantic_recall(self, query: str, top_k: int = 120) -> list[dict]:
        """语义召回:embedding 全库 top-k(召回字面不匹配但语义相关的事实)。无索引则空。"""
        idx = self._semantic_index()
        if idx is None:
            return []
        fids = idx.search(query, top_k=top_k)
        out = [self.facts.get(fid) for fid in fids]
        return [f for f in out if f and f.get("status") in ("active", "disputed")]

    def _semantic_scores(self, query: str, facts: list[dict]) -> dict:
        """候选池语义相似度(0~1);无索引则空 dict。"""
        idx = self._semantic_index()
        if idx is None:
            return {}
        return idx.score(query, [f.get("fact_id") for f in facts])

    def _locate_entity(self, query: str) -> Optional[str]:
        """在注册表里找 query 命中的实体。**个股/公司优先于概念/材料**,但短证券名是更长匹配的子串时不算。

        治两病:① "存储测试设备 精智达 进展"锚到 `concept:存储测试设备`(6字)而非精智达(3字)——证券优先;
        ② "人工智能"被 2 字公司名"智能"(⊂"人工智能")劫持——**证券若只是某更长匹配的子串,则不享优先**
        (精智达非"存储测试设备"子串→优先;智能是"人工智能"子串→不优先)。同档内最长优先。
        M2 修正:纯 ASCII 别名易子串误匹配('pe'⊂'performance'),故 ASCII 要求 >=4 且词边界;中文 >=2 可子串。
        """
        qn = _normalize(query)
        rows = self.registry.conn.execute(
            "SELECT alias_norm, canonical_id FROM aliases ORDER BY LENGTH(alias_norm) DESC"
        ).fetchall()
        matches = []                              # 命中的 (alias, cid, is_security);命中的只有少数几个
        for r in rows:
            a, cid = r["alias_norm"], r["canonical_id"]
            if not a:
                continue
            if a.isascii():
                if not (len(a) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", qn)):
                    continue
            elif not (len(a) >= 2 and a in qn):
                continue
            matches.append((a, cid, _is_security(cid)))
        if not matches:
            return None
        non_sec = [m for m in matches if not m[2]]
        # 合格证券:不是【任一】更长非证券匹配的真子串(排除"智能"⊂"人工智能"式劫持)。
        # 注:须对所有更长非证券匹配判子串,不能只比最长——否则"半导体设备产业 华为概念 华为"里
        # 华为(⊂华为概念)会因不是最长匹配"半导体设备产业"的子串而漏判,仍被错误优先。
        def _hijacked(m):
            return any(len(m[0]) < len(n[0]) and m[0] in n[0] for n in non_sec)
        sec = [m for m in matches if m[2] and not _hijacked(m)]
        if sec:
            return max(sec, key=lambda m: len(m[0]))[1]    # 最长合格证券
        return max(matches, key=lambda m: len(m[0]))[1]    # 无合格证券 → 最长匹配(任意类型)

    def _keyword_facts(self, query: str, limit: int = 20) -> list[dict]:
        """关键词检索:B2 改为 gram 重叠打分(无 jieba 也能处理无空格中文)。"""
        qg = _content_grams(query)
        if not qg:
            return []
        rows = self.facts.query(include_invalidated=False, limit=2000)
        scored = []
        for f in rows:
            fg = _content_grams(f"{f['claim']} {f['object']}")
            score = len(qg & fg)
            if score:
                # 命中 gram 数为主,support_count 次之
                scored.append((score, f["support_count"], f))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [f for _, _, f in scored[:limit]]


def _diversify_by_kind(scored: list) -> list:
    """证据链按 source_kind 轮转,**防单一来源类型垄断头部**。

    治本病:纯股票名查询(如"精智达")下所有事实 ent_hit 打平,A 级官方公告靠 level(=1.0)+时效
    把 B 级券商研报的**投资逻辑**(龙头地位/订单/产能)挤出 active[:8] 证据链——用户看到一堆程序性
    公告,真正有价值的研报论断反被 LLM 综合成"待验证"丢弃。这是把"证据确凿度"误当"信息价值"。
    本函数让研报/公告/社媒**都进证据链前排**:保分数序(out[0] 仍是全局最高分,不破坏 recency 测试),
    每轮各非空来源类按其当前头部分数降序各取一条,使第二高价值来源的最佳论断顶到 F2。

    scored: 已按 score 降序的 [(score, fact), ...]。
    """
    from collections import OrderedDict
    groups: "OrderedDict[str, list]" = OrderedDict()
    for s, f in scored:
        groups.setdefault(f.get("source_kind") or "?", []).append((s, f))
    if len(groups) <= 1:                      # 单一来源类型,无需轮转
        return [f for _, f in scored]
    out = []
    while any(groups.values()):
        # 本轮:每个非空来源类各出一条。类间排序键 =(成色层, -分数):
        # 成色层 0=高信息价值(A 公告 / B·B+ 研报),1=低成色(C 社媒 / D 碎片)——
        # 让券商研报投资逻辑与官方公告优先进前排,C 级谣言不抢 B 级研报的位置;层内按分数。
        def _key(k):
            s, f = groups[k][0]
            tier = 0 if f.get("evidence_level") in ("A", "B+", "B") else 1
            return (tier, -s)
        for k in sorted((k for k in groups if groups[k]), key=_key):
            out.append(groups[k].pop(0)[1])
    return out


def _is_security(cid) -> bool:
    """canonical_id 是否为证券实体(个股代码 SH/SZ/BJ、company:、stock_pending:)。

    用于:① 实体解析时证券优先于概念/材料;② ask 决定是否跑语义召回——证券走精准快路径,
    非证券(概念/材料/指数…)是发现型 topic 查询,需语义召回去找相关个股。
    """
    if not cid:
        return False
    return bool(re.match(r"^(SH|SZ|BJ)\d", cid)) or cid.startswith(("company:", "stock_pending:"))


def _merge_facts(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """合并两组事实,按 fact_id 去重,保序(primary 在前)。B1:实体+关键词召回并集。"""
    seen = set()
    out = []
    for f in list(primary) + list(secondary):
        fid = f["fact_id"]
        if fid in seen:
            continue
        seen.add(fid)
        out.append(f)
    return out


def _recency(valid_at, today_ord: int) -> float:
    """时效权重(P0):1 年内线性 1→0，缺失/解析失败按 0(不加分也不罚)。

    治"老高成色研报永远压新社媒":让最新内容凭时效项也能进结论候选(新定锚、老对照)。
    """
    if not valid_at:
        return 0.0
    try:
        from datetime import date as _Date
        d = _Date.fromisoformat(str(valid_at)[:10]).toordinal()
        age = today_ord - d
        return 1.0 if age < 0 else max(0.0, 1.0 - age / 365.0)
    except Exception:
        return 0.0


# ── 渲染辅助 ──────────────────────────────────────────────────────────────
def _grade_tag(f: dict) -> str:
    lvl = f["evidence_level"]
    unv = "·待验证" if f["unverifiable"] else ""
    return f"[{lvl}级{unv}]"


def _vn(f: dict) -> str:
    import json
    try:
        extra = json.loads(f.get("extra") or "{}")
        return str(extra.get("verified_numbers", 0))
    except Exception:
        return "0"


def _doubts(f: dict) -> list[dict]:
    """取事实的质疑标记列表。"""
    import json
    try:
        return json.loads(f.get("extra") or "{}").get("doubts") or []
    except Exception:
        return []


def _doubt_icon(f: dict) -> str:
    """证据行的质疑图标(按最高严重度)。"""
    import json
    try:
        sev = json.loads(f.get("extra") or "{}").get("doubt_severity")
    except Exception:
        sev = None
    return {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(sev, "")


def _sources(f: dict) -> list[str]:
    import json
    try:
        return json.loads(f.get("sources") or "[]")
    except Exception:
        return []

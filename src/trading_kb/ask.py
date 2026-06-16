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
from .facts_store import FactsStore
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

    def ask(self, query: str, include_invalidated: bool = False) -> AskResult:
        """检索:实体定位 → 结构邻居 → 事实(active + 可选历史)。"""
        result = AskResult(query=query)

        # 1) 从 query 里粗解析实体(命中注册表别名)
        cid = self._locate_entity(query)
        result.canonical_id = cid or ""

        if cid:
            # B1:实体命中也叠加关键词检索,避免只取挂该实体的少数事实导致召回坍缩
            ent_facts = self.facts.query(canonical_id=cid, include_invalidated=False, limit=50)
            kw_facts = self._keyword_facts(query, limit=30)
            result.facts = _merge_facts(ent_facts, kw_facts)
            result.neighbors = self.structure.neighbors(cid)
            if include_invalidated:
                allf = self.facts.query(canonical_id=cid, include_invalidated=True, limit=80)
                result.invalidated_facts = [f for f in allf
                                            if f["status"] in ("superseded", "invalidated", "expired")]
        else:
            # 无法定位实体 → 退化为关键词扫事实 claim(轻量,兜底)
            result.facts = self._keyword_facts(query)
            if result.facts:
                result.warnings.append("未定位到具体实体,已用关键词检索")
            else:
                result.warnings.append("未定位到具体实体,且关键词无命中")

        if not result.facts and not result.neighbors:
            result.warnings.append("证据不足:无匹配事实/关系")
        return result

    def _locate_entity(self, query: str) -> Optional[str]:
        """在注册表里找 query 命中的实体(最长匹配优先)。

        M2 修正:纯 ASCII 别名易子串误匹配(如 'pe' ⊂ 'performance'),
        故 ASCII 别名要求 >=4 且词边界匹配;中文别名 >=2 可子串。
        """
        qn = _normalize(query)
        best = None
        # 仅取可能命中的别名(按长度倒序),避免全表无差别比对(M3)
        rows = self.registry.conn.execute(
            "SELECT alias_norm, canonical_id FROM aliases ORDER BY LENGTH(alias_norm) DESC"
        ).fetchall()
        for r in rows:
            a = r["alias_norm"]
            if not a:
                continue
            if a.isascii():
                # ASCII:要求 >=4 且词边界(避免 pe⊂performance)
                if len(a) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", qn):
                    return r["canonical_id"]
            else:
                if len(a) >= 2 and a in qn:
                    if best is None or len(a) > len(best[0]):
                        best = (a, r["canonical_id"])
        return best[1] if best else None

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

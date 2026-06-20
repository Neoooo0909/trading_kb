"""核心数据结构。

对应 design_final.md:
- Finding   : report_lab 卡片里的一条论断(摄入输入)
- Fact      : 时序事实(§18 Graphiti schema)
- Relation  : 结构关系(§18 LightRAG typed edge)
- SentimentItem : 舆情碎片(§10-bis)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

# ── 分类类别(分流器输出,§7.3 + 量化扩展)────────────────────────────────
Category = Literal["hard_fact", "structure", "quant_fact", "background"]

# ── 成色等级(§19)────────────────────────────────────────────────────────
EvidenceLevel = Literal["A", "B+", "B", "C", "D"]

# ── 事实状态(§18/§10.3 双时态生命周期)──────────────────────────────────
FactStatus = Literal["active", "superseded", "invalidated", "expired", "disputed"]


@dataclass
class Finding:
    """report_lab 卡片里的一条论断,摄入管线的输入单元。"""
    claim: str
    evidence: str = ""
    numbers: list[dict] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    page: Optional[int] = None
    confidence: str = ""
    # 溯源
    doc_id: str = ""
    source_kind: str = "broker_research"   # 默认券商研报
    source_date: str = ""
    broker: str = ""

    @property
    def verified_numbers(self) -> int:
        """report_lab verify 已校验通过(_v=ok/page_fixed)的数字个数。"""
        return sum(1 for n in self.numbers if n.get("_v") in ("ok", "page_fixed"))


@dataclass
class Fact:
    """时序事实(进 facts_store)。schema 见 design_final.md §18。"""
    subject: str
    predicate: str
    object: str
    canonical_id: str = ""
    claim: str = ""
    status: FactStatus = "active"
    evidence_level: EvidenceLevel = "C"
    unverifiable: bool = False
    source_kind: str = "broker_research"
    support_count: int = 1
    sources: list[str] = field(default_factory=list)   # doc_id 列表
    valid_at: str = ""
    invalid_at: Optional[str] = None
    supersedes: list[str] = field(default_factory=list)
    relation_id: str = ""
    category: Category = "hard_fact"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """事实级去重键(§11/§18):canonical_id|predicate|object 归一。"""
        obj = _normalize(self.object)
        cid = self.canonical_id or _normalize(self.subject)
        return f"{cid}|{self.predicate}|{obj}"

    @property
    def fact_id(self) -> str:
        """稳定 id:基于 dedup_key 的确定性哈希(重复执行不重复追加)。"""
        return hashlib.sha1(self.dedup_key.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict:
        d = asdict(self)
        d["sources"] = json.dumps(self.sources, ensure_ascii=False)
        d["supersedes"] = json.dumps(self.supersedes, ensure_ascii=False)
        d["extra"] = json.dumps(self.extra, ensure_ascii=False)
        d["fact_id"] = self.fact_id
        d["dedup_key"] = self.dedup_key
        return d


@dataclass
class Relation:
    """结构关系(进 structure_store)。typed 产业链边,带多篇投票(§18 F6)。"""
    src: str            # canonical_id 或归一名
    rel_type: str       # UPSTREAM_OF / SUPPLIES / COMPETES_WITH / BELONGS_TO_SEGMENT
    dst: str
    support_count: int = 1
    sources: list[str] = field(default_factory=list)
    low_confidence: bool = False

    @property
    def rel_id(self) -> str:
        key = f"{_normalize(self.src)}|{self.rel_type}|{_normalize(self.dst)}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class SentimentItem:
    """舆情碎片(进 sentiment_lane)。§10-bis 轻舆情 lane。"""
    text: str
    canonical_id: str = ""
    stance: str = "neutral"     # bullish / bearish / neutral
    claim: str = ""
    timestamp: str = ""
    source_kind: str = "social_chat"
    evidence_level: EvidenceLevel = "D"
    unverifiable: bool = True
    promoted: bool = False

    @property
    def item_id(self) -> str:
        key = f"{self.canonical_id}|{_normalize(self.claim or self.text)}|{self.timestamp}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _normalize(s: str) -> str:
    """文本归一:去空白/标点/全角,小写。用于去重键。"""
    if not s:
        return ""
    out = s.lower()
    for ch in " \t\n　,，.。、;；:：!！?？\"'“”‘’()（）[]【】":
        out = out.replace(ch, "")
    return out


def content_grams(s: str) -> set[str]:
    """提取内容 gram:英数词(>=2)+ 中文字符 2-gram。

    无 jieba 环境下做中文重叠匹配的统一基元(检索/冲突消解共用)。
    """
    import re
    s = s or ""
    grams = set(re.findall(r"[A-Za-z0-9]{2,}", s.lower()))
    for seg in re.findall(r"[一-鿿]+", s):
        if len(seg) == 1:
            grams.add(seg)
        for i in range(len(seg) - 1):
            grams.add(seg[i:i + 2])
    return grams

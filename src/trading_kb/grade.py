"""双轨成色判定(§10.1 核心修正)。

- 可验证类(订单/中标/产能/交付/价格):走数据对抗验证(verify_hooks),
  查到=升级、查不到=降级。默认 USE_DATA_VERIFY=0 时不实查,按信源基线 + 标 unverifiable。
- 不可验证类(产业链定性/预期差/量化因子):成色 = 信源种类直接映射,
  审核做"一致性检查",查不到 ≠ 证伪,保留基线成色 + unverifiable。

铁律:查不到 ≠ 证伪(§3 铁律5)。
"""
from __future__ import annotations

from typing import Optional

from .models import Finding, Fact, EvidenceLevel

# ── 信源种类 → 基线成色映射(§19)────────────────────────────────────────
SOURCE_KIND_BASELINE: dict[str, EvidenceLevel] = {
    "official_announcement": "A",
    "financial_report": "A",
    "exchange_interaction": "A",
    "government_policy": "A",
    "broker_research": "B",
    "foreign_ib_research": "B+",      # 外资行研报(高盛/大摩/瑞银/JPM等)→ B+,高于国内券商
    "industry_database": "B",
    "company_ir": "B",
    "expert_meeting": "C",
    "media_report": "C",
    "social_research": "C",           # 社媒深度研究(星球/ima 卡片):此前靠默认值得到 C,显式登记防漂移
    "social_chat": "D",
    "market_price": "A",
    "manual_review": "B",
}

# 可验证类 predicate(走数据对抗验证)
VERIFIABLE_PREDICATES = {
    "HAS_CONFIRMED_ORDER", "HAS_DELIVERY_VALIDATION", "HAS_CAPACITY", "HAS_PRICE_SIGNAL",
}


def baseline_grade(source_kind: str) -> EvidenceLevel:
    """信源种类基线成色。未知信源保守给 C。"""
    return SOURCE_KIND_BASELINE.get(source_kind, "C")


def grade_fact(f: Finding, predicate: str, verify=None) -> tuple[EvidenceLevel, bool]:
    """返回 (evidence_level, unverifiable)。

    verify 为可选数据验证钩子,签名 verify(finding, predicate) -> Optional[str]。
    三态协议(docs/ARCHITECTURE.md §2.1,四种合法返回值):
      'confirmed'   查到同主题权威证据 → 升 A
      'refuted'     权威证据打脸       → 降 D
      'no_evidence' 真的查了、没有对应证据 → 降一档(查不到≠证伪)
      None          没查(未接通/异常/该谓词无验证动作) → 保持基线
    历史缺陷:v0.4 前 None 被当"查无"降级,导致空桩钩子让全体可验证事实
    未经任何实查就被降档;新 verifier 禁止把"没查"与"查无"折叠。
    """
    base = baseline_grade(f.source_kind)

    # 不可验证类:信源基线 + 一致性(此处单条,无矛盾即保留),标 unverifiable
    if predicate not in VERIFIABLE_PREDICATES:
        return base, True

    # 可验证类:若无验证钩子,保留基线 + unverifiable(不假装已验证,也不证伪)
    if verify is None:
        return base, True

    result = verify(f, predicate)
    if result == "confirmed":
        return "A", False               # 数据证实 → 升级 A
    if result == "refuted":
        # 数据打脸:交由上层写 CONTRADICTS;此处返回最低成色标记
        return "D", False
    if result == "no_evidence":
        # 实查过且无对应证据:降级一档但不低于 C,保留 unverifiable
        return _downgrade(base), True
    # None = 本条实际没被验证:与"无钩子"同义,保持基线(不假装查过)
    return base, True


def _downgrade(level: EvidenceLevel) -> EvidenceLevel:
    """可验证类查无时温和降级:A→B→C,C/D 不再降(C 已是待验证底线)。"""
    return {"A": "B", "B+": "B", "B": "C", "C": "C", "D": "D"}[level]

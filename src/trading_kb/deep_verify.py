"""深度质疑闭环:可疑硬事实 → 拉公告正文 → 核对说法 vs 真实披露口径。

流程(§质疑 + §联网):
  可疑硬事实(带 stock code)→ 按说法类型匹配公告大类 → 拉公告正文
    → 核对:说法与公告是否一致 → 裁决
      corroborated  公告佐证(说法可信,可清乐观存疑/升级 A)
      contradicted  公告为澄清/风险类且高度相关(说法可能被打脸 → 标 disputed)
      not_disclosed 公告里查无对应披露(乐观存疑维持,提醒"无公告坐实")
      not_applicable 无 code/非硬事实,不适用

核对用规则核心(content_grams 重叠 + 公告大类),可换 LLM 深核。
仅在显式调用(./tkb deep-check)或 USE_WEB 开启时联网。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .models import content_grams

# 说法类型 → 优先核对的公告大类(按 predicate / 关键词)
_CLAIM_TO_CATEGORY = {
    "HAS_CONFIRMED_ORDER": "重大合同/中标",
    "HAS_DELIVERY_VALIDATION": "重大合同/中标",
    "HAS_CAPACITY": "对外投资",
    "HAS_PRICE_SIGNAL": "",        # 价格类无固定大类,全量找
}
# 与"乐观/利好"说法相悖的公告大类(出现即视作打脸信号)
_CONTRADICTING_CATEGORIES = {"澄清/媒体回应", "诉讼/风险/处罚"}

_OVERLAP_MIN = 3   # claim 与公告共享内容 gram 的最小阈值(低于=不相关)


@dataclass
class DeepVerdict:
    status: str                 # corroborated / contradicted / not_disclosed / not_applicable
    matched_title: str = ""
    matched_category: str = ""
    overlap: int = 0
    note: str = ""

    def tag(self) -> str:
        icon = {"corroborated": "🟢", "contradicted": "🔴",
                "not_disclosed": "🟡", "not_applicable": "⚪"}.get(self.status, "•")
        return f"{icon} {self.note}"


def code_from_canonical(cid: str) -> Optional[str]:
    """从 canonical_id 取 6 位证券代码(SH688017→688017);非股票返回 None。"""
    if cid and cid[:2] in ("SH", "SZ", "BJ") and cid[2:].isdigit() and len(cid) == 8:
        return cid[2:]
    return None


def cross_check(claim: str, predicate: str, docs: list[dict],
                subject: str = "", expected_category: str = "") -> DeepVerdict:
    """核对说法与公告正文。docs=[{title,category,text,...}]。

    防假阳性(关键):扣掉公司名(subject)再算重叠,只比"说法特有内容";
    且佐证须落在相关大类(expected_category 或非无关类),避免拿无关公告硬凑佐证。
    """
    if not docs:
        return DeepVerdict("not_disclosed", note="公告中未找到对应披露,乐观说法无公告坐实,存疑维持")

    # 说法特有 gram = claim gram 扣掉公司名 gram(公司名每条公告都含,不算证据)
    subj_g = content_grams(subject) if subject else set()
    claim_g = content_grams(claim) - subj_g
    if not claim_g:
        return DeepVerdict("not_disclosed", note="说法去除主体后无可比内容")

    best = (0, None)
    for d in docs:
        blob = (content_grams(d.get("title", "")) |
                content_grams((d.get("text", "") or "")[:6000])) - subj_g
        ov = len(claim_g & blob)            # 只数"说法特有"重叠
        if ov > best[0]:
            best = (ov, d)
    overlap, d = best
    if d is None or overlap < _OVERLAP_MIN:
        return DeepVerdict("not_disclosed", overlap=overlap,
                           note="公告里无与说法高度相关的披露(已排除公司名干扰),存疑维持")

    cat = d.get("category", "")
    title = d.get("title", "")
    # 澄清/风险类且相关 → 打脸
    if cat in _CONTRADICTING_CATEGORIES:
        return DeepVerdict("contradicted", title, cat, overlap,
                           note=f"公告为「{cat}」且高度相关 → 说法可能被打脸,建议核实")
    # 佐证须落在预期大类(如订单说法须中标/合同类);否则视作未披露
    if expected_category and cat != expected_category:
        return DeepVerdict("not_disclosed", title, cat, overlap,
                           note=f"相关公告为「{cat}」非「{expected_category}」,未直接坐实,存疑维持")
    return DeepVerdict("corroborated", title, cat, overlap,
                       note=f"「{cat}」公告与说法一致 → 获官方披露佐证")


def deep_verify_fact(fact: dict, fetch_fn: Optional[Callable] = None) -> DeepVerdict:
    """对一条事实做深度核对。fact 为 facts_store.query 返回的 dict。

    fetch_fn(code, category)->list[dict];默认用 announcement.fetch_with_text(联网)。
    """
    if fact.get("category") != "hard_fact":
        return DeepVerdict("not_applicable", note="非硬事实,不做公告核对")
    code = code_from_canonical(fact.get("canonical_id", ""))
    if not code:
        return DeepVerdict("not_applicable", note="主体无证券代码,无法定位公告")

    predicate = fact.get("predicate", "")
    cat_hint = _CLAIM_TO_CATEGORY.get(predicate, "")
    fetch = fetch_fn or _default_fetch
    docs = fetch(code, cat_hint)
    return cross_check(fact.get("claim", ""), predicate, docs,
                       subject=fact.get("subject", ""), expected_category=cat_hint)


def _default_fetch(code: str, category: str) -> list[dict]:
    """默认抓取:announcement.fetch_with_text(联网,带正文)。失败返回空。"""
    try:
        from .announcement import fetch_with_text
        # 先按匹配大类找;无命中再全量找
        docs = fetch_with_text(code=code, limit=3, category=category) if category else []
        if not docs:
            docs = fetch_with_text(code=code, limit=5)
        return docs
    except Exception:
        return []

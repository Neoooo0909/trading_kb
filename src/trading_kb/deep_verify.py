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


def deep_verify_fact(fact: dict, fetch_fn: Optional[Callable] = None,
                     allow_soft: bool = False) -> DeepVerdict:
    """对一条事实做深度核对。fact 为 facts_store.query 返回的 dict。

    fetch_fn(code, category)->list[dict];默认用 announcement.fetch_with_text(联网)。
    allow_soft=True 时放开 hard_fact 限制,允许核 quant_fact/structure——供 ask 自动核验
    新鲜低成色线索(如康宁 MOU 多挂 quant_fact/structure 而非 hard_fact),否则会被一刀切跳过。
    """
    if not allow_soft and fact.get("category") != "hard_fact":
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


def _event_grams(claim: str, subject: str = "") -> set:
    """事件归一 gram:剥结构事实的关系前缀「实体」（极性）：、英中名与 MOU 同义归一、去主体名,
    使同一桩事件(康宁×京东方×MOU)的不同措辞/关系(SUPPLIES_TO/BENEFITS/DRIVES…)落到相近 gram 集。"""
    import re
    m = re.search(r"」（[^）]*）：(.+)$", claim)            # 结构事实:取「实体」（极性）：之后的真正描述
    core = m.group(1) if m else claim
    core = core.replace("Corning", "康宁").replace("ＢＯＥ", "京东方").replace("BOE", "京东方")
    core = re.sub(r"(?i)mou|备忘录", "MOU", core)           # MoU/MOU/备忘录/合作备忘录 归一
    g = content_grams(core)
    return (g - content_grams(subject)) if subject else g


def _sim(a: set, b: set) -> float:
    """重叠系数 |a∩b|/min(|a|,|b|):对"短措辞⊂长措辞"更鲁棒(签署MOU后 ⊂ 完整康宁描述)。"""
    return len(a & b) / min(len(a), len(b)) if (a and b) else 0.0


_VERDICT_RANK = {"contradicted": 3, "corroborated": 2, "not_disclosed": 1, "not_applicable": 0}


def auto_verify_fresh(facts: list[dict], today_ord: int, *, max_n: int = 5,
                      max_age_days: int = 120, fetch_fn: Optional[Callable] = None,
                      sim_threshold: float = 0.3) -> list[tuple]:
    """挑"新鲜+低成色+实质"的线索,按事件聚类去重,每事件拉一次公告取最佳佐证(供 ask 自动调用)。

    成色≠时效价值:新低成色边际信息(康宁 MOU 等)对当下定价有意义但需独立核实,本函数自动化这步。
    选取:成色 C/D + valid_at 近 max_age_days 天 + 有证券码 + claim 实质(≥10字)。
    去重(治本):同股 + 事件 gram 重叠系数≥sim_threshold 视作同一桩事件(康宁的 SUPPLIES_TO/
      BENEFITS/DRIVES/MOU/合作备忘录 等多条措辞合一),只算一个事件;按新鲜度取前 max_n 个事件。
    核验:每事件拉一次公告,对其全部措辞各跑 cross_check 取最佳结论(打脸>佐证>存疑——打脸必报、
      佐证优先于存疑),代表取最新一条措辞。返回 [(代表fact, DeepVerdict, 合并条数), ...](剔 not_applicable)。
    成本=至多 max_n 次公告拉取(纯文本比对)。
    """
    from datetime import date as _Date

    elig = []
    for f in facts:
        if f.get("evidence_level") not in ("C", "D"):
            continue
        if not code_from_canonical(f.get("canonical_id", "")):
            continue
        claim = (f.get("claim") or "").strip()
        if len(claim) < 10:
            continue
        va = f.get("valid_at")
        if not va:
            continue
        try:
            age = today_ord - _Date.fromisoformat(str(va)[:10]).toordinal()
        except Exception:
            continue
        if age < 0 or age > max_age_days:                 # 太老/日期异常,跳过
            continue
        elig.append((age, f, _event_grams(claim, f.get("subject", ""))))

    elig.sort(key=lambda x: x[0])                         # 越新越靠前(代表=事件内最新)

    clusters = []                                         # [{code, sig, rep, members:[fact]}]
    for _age, f, sig in elig:
        code = code_from_canonical(f.get("canonical_id", ""))
        hit = next((cl for cl in clusters
                    if cl["code"] == code and len(sig & cl["sig"]) >= 3       # 绝对交集地板:防短句病态误并
                    and _sim(sig, cl["sig"]) >= sim_threshold), None)
        if hit:
            hit["members"].append(f)
        else:
            clusters.append({"code": code, "sig": sig, "rep": f, "members": [f]})

    fetch = fetch_fn or _default_fetch
    out = []
    for cl in clusters[:max_n]:                           # 取最新的 max_n 个事件
        docs = fetch(cl["code"], "")
        best_v = None
        for m in cl["members"]:                           # 同事件全部措辞各核,取最佳结论
            v = cross_check(m.get("claim", ""), m.get("predicate", ""), docs,
                            subject=m.get("subject", ""))
            if best_v is None or _VERDICT_RANK.get(v.status, 0) > _VERDICT_RANK.get(best_v.status, 0):
                best_v = v
        if best_v and best_v.status != "not_applicable":
            out.append((cl["rep"], best_v, len(cl["members"])))
    return out

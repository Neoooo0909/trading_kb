"""联网权威信源佐证(只采信权威:公告/交易所披露)。

设计原则(用户要求):**联网只采信权威信源**,非权威一律不采信。
数据源:巨潮资讯 cninfo.com.cn(证监会指定法定披露平台,沪深两市全量,
免费无额度,首选);沪市兜底上交所。tdx 无公告不走;不接 hibor;
智能选股 smart_stock_picking 有月限额 4000/月,不用于公告。

对外提供两个钩子(默认安全桩,离线可复现;TKB_USE_WEB=1 接真实源):
  make_announcement_verifier() → 给 grade 用,遵守三态协议(ARCHITECTURE.md §2.1)
  make_corroborator()          → 给 critique 用:乐观结论有无权威佐证

confirmed 判据(v0.5 修正):公告标题必须命中**谓词对应的主题关键词**。
历史缺陷:v0.4 前"该公司存在任意公告"即返回 confirmed → 研报订单说法被
股东大会通知之类的无关公告洗白成 A 级,与设计意图完全相反,已修正。
"""
from __future__ import annotations

from typing import Optional

from . import config
from .models import Finding

# ── 谓词 → 公告标题主题关键词(命中任一才算"同主题公告")────────────────
# HAS_PRICE_SIGNAL 不在表内:价格信号无法用公告验证,verifier 对它返回 None(未尝试)。
_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "HAS_CONFIRMED_ORDER":     ("中标", "重大合同", "重大订单", "采购合同", "框架协议",
                                "中选", "签订", "订单"),
    "HAS_CAPACITY":            ("产能", "扩产", "投产", "达产", "募投", "投资建设",
                                "新建", "扩建"),
    "HAS_DELIVERY_VALIDATION": ("交付", "验收", "量产", "批量供货", "供货"),
}


# ── 对外钩子 ──────────────────────────────────────────────────────────────
def make_announcement_verifier():
    """给 grade 用的验证器(三态协议:confirmed/no_evidence/None)。"""
    if not config.USE_WEB:
        return None
    return _verify_via_authoritative


def make_corroborator():
    """给 critique 用:乐观结论是否获权威公告佐证 → 'corroborated' / None。

    佐证判据与 verifier 同源:结论文本里出现的主题词,须在公告标题中命中;
    "该公司存在任意公告"不构成佐证。
    """
    if not config.USE_WEB:
        return None

    def _corroborate(f: Finding, metric: str) -> Optional[str]:
        # 结论里出现了哪些可核对的主题词(量化指标类结论公告无法佐证 → None)
        claim = f.claim or ""
        kws = tuple(k for group in _TOPIC_KEYWORDS.values() for k in group if k in claim)
        if not kws:
            return None
        anns = _fetch_recent(f)
        if anns and any(any(k in a.title for k in kws) for a in anns):
            return "corroborated"
        return None
    return _corroborate


# ── 验证逻辑(事件驱动,仅可验证类硬事实)──────────────────────────────────
def _verify_via_authoritative(f: Finding, predicate: str) -> Optional[str]:
    """公告验证,三态协议(ARCHITECTURE.md §2.1):

      'confirmed'   公告标题命中该谓词的主题关键词
      'no_evidence' 查询成功、拿到了公告列表、但无同主题公告(真查无 → grade 降档)
      None          未尝试:该谓词无公告验证动作 / 请求异常 / 空结果
                    (空结果无法区分"真没公告"与"接口静默失败",按未尝试处理,不降级)
    """
    kws = _TOPIC_KEYWORDS.get(predicate)
    if not kws:
        return None
    anns = _fetch_recent(f)
    if not anns:
        return None
    if any(any(k in a.title for k in kws) for a in anns):
        return "confirmed"
    return "no_evidence"


def _fetch_recent(f: Finding) -> list:
    """拉该主体近期公告列表(巨潮主 + 上交所兜底)。异常/未开启 → 空列表。"""
    if not config.USE_WEB or not f.entities:
        return []
    try:
        from .announcement import fetch_announcements
        return fetch_announcements(f.entities[0], limit=30)
    except Exception as e:
        import sys
        print(f"[web_enrich] 公告查询失败({type(e).__name__}),按未尝试处理: "
              f"{f.entities[0]}", file=sys.stderr)
        return []

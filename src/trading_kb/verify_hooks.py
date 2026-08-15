"""数据源验证钩子(§8/§19 可验证类)。

verify 三态协议见 docs/ARCHITECTURE.md §2.1:
  'confirmed' / 'refuted' / 'no_evidence'(真查了没查到→降级) / None(没查→保持基线)。
默认 USE_DATA_VERIFY=0:make_verifier 返回 None,grade 走信源基线 + unverifiable。
启用后接 巨潮cninfo(公告,免费)/ ifind_ft(财务) / tdx(异动) 实查。
注:智能选股有月限额 4000/月,公告改走巨潮免费源(见 web_enrich)。

设计:验证是"事件驱动"——仅可验证类 predicate 才触发,控制额度(§23.1)。
此处实现安全桩:子检查未接通时返回 None(=未尝试,不降级)。绝不抛错、
绝不假装确认、也绝不假装查过(历史缺陷:None 曾被 grade 当"查无"降级,
导致空桩让全体可验证事实未经实查就降档,已按三态协议修正)。
"""
from __future__ import annotations

from typing import Optional

from . import config
from .models import Finding


def make_verifier():
    """返回验证函数 verify(finding, predicate)->Optional[str],或 None(不验证)。

    返回 None 表示"本管线不做数据验证",grade 将走信源基线。
    """
    if not config.USE_DATA_VERIFY:
        return None
    return _live_verify


def _live_verify(f: Finding, predicate: str) -> Optional[str]:
    """实查(仅在 USE_DATA_VERIFY=1 时调用)。

    返回三态协议值:'confirmed' / 'refuted' / 'no_evidence' / None(未尝试)。
    异常一律折叠为 None(未尝试,不降级、不阻塞、不编造)。
    """
    try:
        # 事件驱动:按 predicate 选验证动作(§19 验证动作映射)
        if predicate in ("HAS_CONFIRMED_ORDER",):
            return _check_announcement(f)
        if predicate in ("HAS_CAPACITY", "HAS_DELIVERY_VALIDATION"):
            return _check_financials(f)
        if predicate in ("HAS_PRICE_SIGNAL",):
            return _check_research(f)
    except Exception:
        return None
    return None


def _check_announcement(f: Finding) -> Optional[str]:
    """查公告/中标:优先巨潮 cninfo(免费无额度)。

    安全桩:未接通实查,返回 None(=未尝试)。接通后实查须区分:
    查到同主题公告→'confirmed';查询成功但无对应→'no_evidence';请求失败→None。
    """
    return None


def _check_financials(f: Finding) -> Optional[str]:
    """查在建工程/合同负债(ifind_ft.hxfs)。安全桩:返回 None(=未尝试)。"""
    return None


def _check_research(f: Finding) -> Optional[str]:
    """查研报报价印证(hibor)。安全桩:返回 None(=未尝试)。"""
    return None

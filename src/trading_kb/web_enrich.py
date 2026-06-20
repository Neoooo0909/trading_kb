"""联网权威信源佐证(只采信权威:公告/交易所/投行/权威媒体)。

设计原则(用户要求):**联网只采信权威信源**,非权威一律不采信。
信任分级:
  A 级(最高):公司公告、交易所互动易、政府/监管文件   —— 可坐实事实
  B 级:券商/投行研报、财联社、证券时报、中证报等权威财经媒体 —— 可佐证
  其余(自媒体/论坛/小作文):**不采信**,直接丢弃

可插拔抓取器(默认安全桩,离线可复现;TKB_USE_WEB=1 接真实源):
  - 公告/中标/产能 → 巨潮资讯 cninfo.com.cn(证监会指定法定披露平台,沪深两市全量,
                    **免费无额度**,首选,权威 A);付费兜底 iFinD report_query(有额度)
  - 权威媒体      → 白名单域名抓取(仅 _AUTHORITATIVE_MEDIA 内,权威 B)

注:① tdx 是纯行情工具(无公告),公告不走 tdx;② 不接 hibor 研报;
   ③ 智能选股 smart_stock_picking 有月限额 4000/月,不用于公告(改走巨潮免费)。

对外提供两个钩子:
  make_announcement_verifier() → 给 grade 用:查到公告把可验证事实升级 A
  make_corroborator()          → 给 critique 用:乐观结论有无权威佐证
"""
from __future__ import annotations

from typing import Optional

from . import config
from .models import Finding

# ── 权威信源白名单与信任分级 ──────────────────────────────────────────────
SOURCE_TRUST = {
    # A 级:可坐实
    "announcement": "A",          # 公司公告
    "exchange_interaction": "A",  # 互动易/上证e互动
    "government": "A",            # 监管/政府文件
    # B+ 级:外资行研报(高于国内券商)
    "foreign_ib_research": "B+",  # 高盛/大摩/瑞银/JPM/美银/伯恩斯坦等
    # B 级:可佐证
    "broker_research": "B",       # 券商/投行研报
    "authoritative_media": "B",   # 权威财经媒体(白名单)
}

# 权威财经媒体白名单(只采信这些域名的"媒体"信息)
_AUTHORITATIVE_MEDIA = {
    "cls.cn",          # 财联社
    "stcn.com",        # 证券时报
    "cs.com.cn",       # 中国证券报
    "sse.com.cn",      # 上交所
    "szse.cn",         # 深交所
    "csrc.gov.cn",     # 证监会
    "cninfo.com.cn",   # 巨潮资讯(公告)
}


def is_authoritative(domain: str) -> bool:
    """域名是否在权威媒体白名单内(非白名单一律不采信)。"""
    d = (domain or "").lower().lstrip("www.")
    return any(d == w or d.endswith("." + w) for w in _AUTHORITATIVE_MEDIA)


# ── 对外钩子 ──────────────────────────────────────────────────────────────
def make_announcement_verifier():
    """给 grade 用的验证器:查到权威公告 → 'confirmed';否则 None(查无≠假)。"""
    if not config.USE_WEB:
        return None
    return _verify_via_authoritative


def make_corroborator():
    """给 critique 用:某结论是否获权威佐证 → 'corroborated' / None。"""
    if not config.USE_WEB:
        return None

    def _corroborate(f: Finding, metric: str) -> Optional[str]:
        try:
            # 只用权威公告佐证(不接 hibor 研报)
            return "corroborated" if _query_announcement(f) else None
        except Exception:
            return None
    return _corroborate


# ── 验证逻辑(事件驱动,仅可验证类硬事实)──────────────────────────────────
def _verify_via_authoritative(f: Finding, predicate: str) -> Optional[str]:
    """只用权威公告验证(订单/中标/产能/交付均查公告)。异常/查无 → None,绝不假装确认。"""
    try:
        if predicate in ("HAS_CONFIRMED_ORDER", "HAS_CAPACITY", "HAS_DELIVERY_VALIDATION"):
            return "confirmed" if _query_announcement(f) else None
    except Exception:
        return None
    return None


# ── 真实抓取器(默认安全桩;接通后只返回权威信源命中)──────────────────────
def _query_announcement(f: Finding) -> bool:
    """查公司公告/中标(权威 A)。

    首选巨潮资讯 cninfo(法定披露平台,沪深全量,免费无额度);付费兜底 iFinD report_query。
    tdx 无公告不走;不接 hibor;智能选股(4000/月)不用于公告。
    安全桩:依赖不可用/未开启时返回 False,绝不编造。
    """
    if not config.USE_WEB or not f.entities:
        return False
    try:
        # 真实抓取:巨潮主 + 沪市兜底上交所(announcement 模块,免费无额度,直连)
        from .announcement import has_announcement
        return has_announcement(f.entities[0])
    except Exception:
        return False


def fetch_authoritative_media(query: str, max_items: int = 5) -> list[dict]:
    """从权威媒体白名单抓取(只返回白名单域名结果)。默认安全桩返回空。

    接入时:对每条结果用 is_authoritative(domain) 过滤,非白名单丢弃。
    """
    if not config.USE_WEB:
        return []
    return []   # 安全桩;真实抓取需接 HTTP + 白名单过滤

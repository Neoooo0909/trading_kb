"""公告抓取测试(mock HTTP,离线可复现;另含可跳过的真实联网冒烟)。"""
import json
import os
import pytest

from trading_kb import announcement as ann


# ── 路由 ──────────────────────────────────────────────────────────────────
def test_plate_routing():
    assert ann._plate_of("600519") == "sh"
    assert ann._plate_of("300750") == "sz"
    assert ann._plate_of("000001") == "sz"
    assert ann._plate_of("900901") == "sh"


def test_ts_to_date():
    assert ann._ts_to_date(1717459200000).startswith("2024")
    assert ann._ts_to_date(None) == ""
    assert ann._ts_to_date("bad") == ""


# ── 巨潮解析(mock)────────────────────────────────────────────────────────
def test_query_cninfo_parse(monkeypatch):
    fake = json.dumps({"announcements": [
        {"announcementTitle": "中标重大合同公告", "announcementTime": 1717459200000,
         "adjunctUrl": "finalpage/2026-06-04/123.PDF"}]})
    monkeypatch.setattr(ann, "_http", lambda *a, **k: fake)
    res = ann.query_cninfo("某公司", plate="sz")
    assert len(res) == 1
    assert res[0].title == "中标重大合同公告"
    assert res[0].url.startswith("http://static.cninfo.com.cn/")
    assert res[0].source == "cninfo"


def test_query_cninfo_empty_on_fail(monkeypatch):
    monkeypatch.setattr(ann, "_http", lambda *a, **k: None)   # 网络失败
    monkeypatch.setattr(ann.time, "sleep", lambda *a: None)   # 跳过退避等待
    assert ann.query_cninfo("X") == []


# ── 上交所解析(mock)──────────────────────────────────────────────────────
def test_query_sse_parse(monkeypatch):
    fake = json.dumps({"pageHelp": {"data": [
        {"TITLE": "沪市公告", "SSEDATE": "2026-06-10", "URL": "/disclosure/a.pdf"}]}})
    monkeypatch.setattr(ann, "_http", lambda *a, **k: fake)
    res = ann.query_sse("600519")
    assert res[0].title == "沪市公告"
    assert res[0].url == "http://www.sse.com.cn/disclosure/a.pdf"
    assert res[0].source == "sse"


# ── 兜底路由:巨潮空 + 沪市票 → 转上交所 ──────────────────────────────────
def test_fallback_to_sse_for_sh(monkeypatch):
    monkeypatch.setattr(ann, "query_cninfo", lambda *a, **k: [])      # 巨潮拿不到
    called = {}
    def fake_sse(code, page_size=10):
        called["code"] = code
        return [ann.Announcement("沪兜底", "2026-06-10", "u", "sse")]
    monkeypatch.setattr(ann, "query_sse", fake_sse)
    res = ann.fetch_announcements("茅台", "600519")
    assert res and res[0].source == "sse"
    assert called["code"] == "600519"


def test_no_fallback_for_sz(monkeypatch):
    """深市票巨潮拿不到时不转上交所(深市公告本就在巨潮)。"""
    monkeypatch.setattr(ann, "query_cninfo", lambda *a, **k: [])
    monkeypatch.setattr(ann, "query_sse", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用")))
    assert ann.fetch_announcements("比亚迪", "002594") == []


# ── has_announcement ──────────────────────────────────────────────────────
def test_has_announcement_keyword(monkeypatch):
    monkeypatch.setattr(ann, "fetch_announcements",
                        lambda *a, **k: [ann.Announcement("公司中标特斯拉", "d", "u", "cninfo")])
    assert ann.has_announcement("X", keyword="中标") is True
    assert ann.has_announcement("X", keyword="减持") is False
    assert ann.has_announcement("X") is True


# ── 公告分类 ──────────────────────────────────────────────────────────────
def test_classify_title():
    assert ann.classify_title("贵州茅台2024年年度报告") == "定期报告"
    assert ann.classify_title("关于股东减持股份的公告") == "股东增减持"
    assert ann.classify_title("关于中标重大合同的公告") == "重大合同/中标"
    assert ann.classify_title("2026年半年度业绩预告") == "业绩预告/快报"
    assert ann.classify_title("关于回购公司股份的进展公告") == "股份回购"
    assert ann.classify_title("关于诉讼事项的公告") == "诉讼/风险/处罚"
    assert ann.classify_title("某个无法归类的奇怪标题") == "其他"


def test_category_filter(monkeypatch):
    fake = [ann.Announcement("年度报告", "d", "u", "cninfo", category="定期报告"),
            ann.Announcement("减持公告", "d", "u", "cninfo", category="股东增减持")]
    monkeypatch.setattr(ann, "query_cninfo", lambda *a, **k: fake)
    res = ann.fetch_announcements("X", "000001", category="股东增减持")
    assert len(res) == 1 and res[0].category == "股东增减持"


# ── orgId 解析(mock 股票表)────────────────────────────────────────────
def test_resolve_orgid(monkeypatch):
    monkeypatch.setattr(ann, "_STOCK_MAP", {"000001": {"orgId": "gssz0000001", "name": "平安银行"}})
    assert ann.resolve_orgid("000001") == "gssz0000001"
    assert ann.resolve_orgid("SZ000001") == "gssz0000001"   # 带前缀也能解
    assert ann.resolve_orgid("999999") is None


def test_cninfo_uses_orgid_precise(monkeypatch):
    """给 code 且能解析 orgId 时,用 stock=code,orgId 精确查询(不靠公司名)。"""
    monkeypatch.setattr(ann, "_STOCK_MAP", {"600519": {"orgId": "gssh0600519", "name": "贵州茅台"}})
    captured = {}
    def fake_http(url, *, data=None, headers=None, timeout=15):
        captured["data"] = data.decode() if data else ""
        return json.dumps({"announcements": []})
    monkeypatch.setattr(ann, "_http", fake_http)
    ann.query_cninfo(keyword="茅台", code="600519")
    assert "600519%2Cgssh0600519" in captured["data"] or "600519,gssh0600519" in captured["data"]


# ── 真实联网冒烟(默认跳过;TKB_LIVE=1 时真打巨潮)─────────────────────────
@pytest.mark.skipif(os.environ.get("TKB_LIVE") != "1", reason="需 TKB_LIVE=1 真实联网")
def test_live_cninfo():
    res = ann.fetch_announcements("贵州茅台", "600519", limit=3)
    assert res and all(a.url.endswith(".PDF") or ".pdf" in a.url.lower() for a in res)
    assert all(a.category for a in res)            # 都有分类


@pytest.mark.skipif(os.environ.get("TKB_LIVE") != "1", reason="需 TKB_LIVE=1 真实联网")
def test_live_orgid_and_text():
    assert ann.resolve_orgid("600519")             # 能解析茅台 orgId
    docs = ann.fetch_with_text("贵州茅台", "600519", limit=1)
    assert docs and docs[0]["text_chars"] > 100    # 提取到正文

"""P1 假设追踪层测试。"""
from trading_kb.hypothesis import HypothesisStore, append_friction


def test_new_and_get(tmp_path):
    hs = HypothesisStore(tmp_path)
    hid = hs.new("精智达净利率能否兑现25%", ticker="688627", statement="研报假设25%，实际5.8%")
    assert hid == "H001"
    txt = hs.get(hid)
    assert "精智达净利率" in txt and "688627" in txt and "研报假设25%" in txt


def test_evidence_updates_confidence(tmp_path):
    hs = HypothesisStore(tmp_path)
    hid = hs.new("假设X")
    assert hs.list_all()[0]["confidence"] == "0.50"          # 无证据先验
    c1 = hs.add_evidence(hid, "财报净利率仅5.8%", side="against", grade="A")
    assert c1 < 0.5                                          # A级反证→置信度跌
    c2 = hs.add_evidence(hid, "Q1高增长", side="for", grade="C")
    assert c2 > c1                                           # 加正证→回升
    txt = hs.get(hid)
    assert "财报净利率仅5.8%" in txt and "[against][A]" in txt and "[for][C]" in txt


def test_list_and_n_evidence(tmp_path):
    hs = HypothesisStore(tmp_path)
    h1 = hs.new("A")
    h2 = hs.new("B")
    hs.add_evidence(h1, "e1", "for", "B")
    hs.add_evidence(h1, "e2", "against", "C")
    rows = {r["id"]: r for r in hs.list_all()}
    assert rows[h1]["n_evidence"] == 2
    assert rows[h2]["n_evidence"] == 0
    assert h2 == "H002"                                      # 自增 id


def test_resolve(tmp_path):
    hs = HypothesisStore(tmp_path)
    hid = hs.new("假设")
    hs.resolve(hid, "refuted", "证伪：净利率未兑现")
    txt = hs.get(hid)
    assert "status: refuted" in txt
    assert "[refuted] 证伪：净利率未兑现" in txt
    assert hs.list_all()[0]["status"] == "refuted"


def test_friction_log(tmp_path):
    append_friction(tmp_path, "jgbsessid 失效")
    append_friction(tmp_path, "tkb 召回偏题")
    txt = (tmp_path / "friction-log.md").read_text(encoding="utf-8")
    assert "jgbsessid 失效" in txt and "tkb 召回偏题" in txt
    assert txt.count("- [") == 2


def test_title_with_newline_no_frontmatter_injection(tmp_path):
    """🟡回归:title 含换行不得被 _parse 误拆成额外 frontmatter 键。"""
    hs = HypothesisStore(tmp_path)
    hid = hs.new("正常标题\nstatus: refuted\nfake: x")    # 注入企图
    fm = hs.list_all()[0]
    assert "fake" not in fm                               # 未注入伪键
    assert fm["status"] == "open"                         # status 未被篡改
    assert fm["title"].startswith("正常标题")              # 标题折叠为单行保留

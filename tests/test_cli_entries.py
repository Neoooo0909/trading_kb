"""新入口命令测试:feed-chat 的碎片解析 + watch_terms 标的池派生。"""
from trading_kb.cli import _read_fragments


def test_watch_terms_only_stocks(tmp_registry):
    """watch_terms 默认只取股票实体,概念/已合并的不进池。"""
    tmp_registry.register("绿的谐波", type_="stock", stock_code="688017")
    tmp_registry.register("宁德时代", type_="stock", stock_code="300750")
    tmp_registry.register("机器人", type_="concept")          # 概念不进池
    terms = tmp_registry.watch_terms()
    assert "绿的谐波" in terms and "宁德时代" in terms
    assert "机器人" not in terms


def test_watch_terms_skips_merged(tmp_registry):
    """已 merged_into 的实体不再作关注标的(避免用废名过滤)。"""
    a = tmp_registry.register("绿的谐波", type_="stock", stock_code="688017")
    b = tmp_registry.register("绿谐", type_="stock", stock_code="688017")
    # 同 code → 同 canonical_id,本就是一个;构造一个真正的废名实体再合并
    c = tmp_registry.register("旧名公司", type_="stock", stock_code="000001")
    tmp_registry.merge(c, a)
    terms = tmp_registry.watch_terms()
    assert "旧名公司" not in terms


def test_read_fragments_plain(tmp_path):
    """无时间戳:每行一条,时间戳留空。"""
    p = tmp_path / "chat.txt"
    p.write_text("绿的谐波要起飞了\n\n宁德时代利空\n", encoding="utf-8")
    frags = _read_fragments(p)
    assert frags == [("绿的谐波要起飞了", ""), ("宁德时代利空", "")]


def test_read_fragments_with_timestamp(tmp_path):
    """行首带时间戳:多种分隔符都能切出 (文本, 时间戳)。"""
    p = tmp_path / "chat.txt"
    p.write_text(
        "2026-06-10 09:30 绿的谐波要起飞\n"
        "[2026-06-11 14:00] 宁德时代加单传闻\n"
        "2026-06-12\t贵州茅台稳\n",
        encoding="utf-8",
    )
    frags = _read_fragments(p)
    assert frags[0] == ("绿的谐波要起飞", "2026-06-10 09:30")
    assert frags[1] == ("宁德时代加单传闻", "2026-06-11 14:00")
    assert frags[2] == ("贵州茅台稳", "2026-06-12")


def test_read_fragments_pure_date_lookalike_not_eaten(tmp_path):
    """正文以普通文字开头不会被误判时间戳。"""
    p = tmp_path / "chat.txt"
    p.write_text("机器人板块今天很强\n", encoding="utf-8")
    frags = _read_fragments(p)
    assert frags == [("机器人板块今天很强", "")]


def test_read_fragments_missing_file(tmp_path):
    assert _read_fragments(tmp_path / "nope.txt") == []

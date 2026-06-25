"""P0 检索召回回归测试(固化"已知该召回什么"，防回归)。

锚定今天精智达翻车的根因修复:
① 召回不坍缩(实体命中 + LIKE，不受前 2000 限制)
② 实体精确命中(精智达 → company:精智达)
③ 时效排序(新内容能进结论候选，不被老高成色压死)
④ 成色排序 bug 修复(evidence_level CASE，A 在 D 前)
⑤ 噪声隔离(不召回茅台)
"""
from trading_kb.ask import AskEngine
from trading_kb.models import Fact


def _seed(reg, facts):
    """造一批事实:精智达(新/旧/不同成色) + 板块映射 + 茅台噪声。"""
    cid = reg.register("精智达", type_="company", stock_code="688627")
    facts.upsert(Fact(subject="精智达", predicate="HAS_ORDER", object="长鑫13亿大订单",
                      canonical_id=cid, claim="精智达在长鑫获得首批13亿大订单",
                      evidence_level="C", valid_at="2026-06-17", sources=["s1"]))
    facts.upsert(Fact(subject="精智达", predicate="HAS_TECH", object="DRAM全流程测试",
                      canonical_id=cid, claim="精智达是国内唯一DRAM全流程测试厂商",
                      evidence_level="B", valid_at="2025-09-18", sources=["s2"]))
    facts.upsert(Fact(subject="精智达", predicate="ANNOUNCE", object="减持公告",
                      canonical_id=cid, claim="精智达股东减持股份结果公告",
                      evidence_level="A", valid_at="2026-06-18", sources=["s3"]))
    facts.upsert(Fact(subject="台积电", predicate="DRIVES", object="半导体设备",
                      canonical_id="concept:半导体设备",
                      claim="台积电扩产带动A股半导体设备采购",
                      evidence_level="C", valid_at="2026-05-01", sources=["s4"]))
    facts.upsert(Fact(subject="贵州茅台", predicate="HAS_PRICE", object="白酒涨价",
                      canonical_id="company:贵州茅台", claim="贵州茅台白酒价格上行",
                      evidence_level="A", valid_at="2026-06-18", sources=["s5"]))
    return cid


def test_entity_recall_and_no_collapse(tmp_registry, tmp_facts, tmp_structure):
    """实体精确命中 + 召回全部精智达事实 + 不召回茅台噪声。"""
    cid = _seed(tmp_registry, tmp_facts)
    res = AskEngine(tmp_registry, tmp_facts, tmp_structure).ask("精智达")
    assert res.canonical_id == cid
    claims = [f["claim"] for f in res.facts]
    assert sum("精智达" in c for c in claims) >= 3       # 三条精智达事实全召回
    assert not any("茅台" in c for c in claims)          # 噪声隔离


def test_recency_lifts_new_over_old(tmp_registry, tmp_facts, tmp_structure):
    """时效项让最新内容进结论候选(治"老高成色研报永远压新")。"""
    _seed(tmp_registry, tmp_facts)
    res = AskEngine(tmp_registry, tmp_facts, tmp_structure).ask("精智达")
    assert res.facts[0].get("valid_at", "") >= "2026-01-01"   # top 不是 2025 老研报


def test_evidence_level_order_fixed(tmp_facts):
    """query 默认成色排序:A 在 D 前(修字符串 DESC 把 D 排前的 bug)。"""
    for lvl in ["D", "A", "C", "B"]:
        tmp_facts.upsert(Fact(subject="X", predicate="P", object=lvl, canonical_id="c:x",
                              claim=f"事实{lvl}", evidence_level=lvl, sources=["s"]))
    levels = [r["evidence_level"] for r in tmp_facts.query(canonical_id="c:x", limit=10)]
    assert levels[0] == "A" and levels[-1] == "D"


def test_search_not_limited_to_2000(tmp_facts):
    """search 用 SQL LIKE 预筛，目标事实即使在大库尾部也能召回(治坍缩)。"""
    # 灌 2500 条噪声(成色 A，旧排序会把它们全堆前面)，目标 1 条埋最后
    for i in range(2500):
        tmp_facts.upsert(Fact(subject="N", predicate="P", object=f"噪声{i}", canonical_id="c:n",
                              claim=f"无关噪声事实编号{i}", evidence_level="A",
                              valid_at="2020-01-01", sources=["n"]))
    tmp_facts.upsert(Fact(subject="标的", predicate="P", object="埋底事实", canonical_id="c:t",
                          claim="稀有关键词鳑鲏鱼出现在此条", evidence_level="C", sources=["t"]))
    hits = tmp_facts.search("鳑鲏鱼", limit=50)
    assert any("鳑鲏鱼" in r["claim"] for r in hits)     # LIKE 预筛能捞到，不受 2000 坍缩


def test_search_with_like_wildcard_chars(tmp_facts):
    """🟡回归:含 %/_ 的查询经 ESCAPE 转义后仍能正确召回字面匹配(转义没破坏正常召回)。

    (% 当通配符的过度召回无法端到端隔离——gram 分词本就会按子串桥接相近 claim；
     此处守住"转义不误伤正常召回"这一可证不变量。)
    """
    tmp_facts.upsert(Fact(subject="A", predicate="P", object="x", canonical_id="c:a",
                          claim="毛利率50%以上的高成长标的", evidence_level="B", sources=["s"]))
    hits = tmp_facts.search("毛利率50%以上", limit=50)
    assert any("50%以上" in r["claim"] for r in hits)     # 字面含 % 的目标仍被召回


def test_entity_precision_not_buried_by_keyword_stuffing(tmp_registry, tmp_facts, tmp_structure):
    """🟠回归:实体精确命中不被"关键词堆砌但无实体"的长文压到后面(排序信号归一)。

    旧式裸 gram 重叠无上界 → 堆砌项 relevance 远超实体命中(=3)，把实体事实沉底。
    归一后字面≤1.5、实体=2.0，实体精确命中应稳居前列。
    """
    cid = tmp_registry.register("精智达", type_="company", stock_code="688627")
    tmp_facts.upsert(Fact(subject="精智达", predicate="HAS_ORDER", object="长鑫订单",
                          canonical_id=cid, claim="精智达获长鑫首批订单",
                          evidence_level="C", valid_at="2026-06-17", sources=["s1"]))
    # 无实体、堆砌查询关键词的噪声长文
    tmp_facts.upsert(Fact(subject="行业", predicate="P", object="测试设备",
                          canonical_id="concept:存储测试",
                          claim="国产存储测试设备龙头份额第一存储测试设备国产替代存储测试设备",
                          evidence_level="A", valid_at="2026-06-18", sources=["s2"]))
    res = AskEngine(tmp_registry, tmp_facts, tmp_structure).ask("精智达 国产存储测试设备 龙头 份额")
    assert res.facts[0]["canonical_id"] == cid            # 实体命中稳居首位，未被堆砌长文压下

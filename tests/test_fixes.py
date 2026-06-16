"""审查修复回归测试:C1 supersede 自碰撞、C2 状态机入管线、M1 代码路由、M2 ask 误配。"""
import pytest

from trading_kb.entity_registry import EntityRegistry, _to_market_code
from trading_kb.facts_store import FactsStore
from trading_kb.structure_store import StructureStore
from trading_kb.ingest import ResearchIngestor, IngestReport, _object_overlap
from trading_kb.ask import AskEngine
from trading_kb.models import Fact


# ── C1:supersede 同 dedup_key 不丢事实 ──────────────────────────────────
def test_supersede_same_key_revives(tmp_facts):
    f = Fact(subject="A", predicate="HAS_CONFIRMED_ORDER", object="订单X",
             canonical_id="SH600000", claim="确认订单X", evidence_level="A",
             sources=["d1"], valid_at="2026-05-01")
    fid = tmp_facts.upsert(f)
    # 同一事实再次确认(同 fact_id)走 supersede:不能把自己标没
    same = Fact(subject="A", predicate="HAS_CONFIRMED_ORDER", object="订单X",
                canonical_id="SH600000", claim="确认订单X", evidence_level="A",
                sources=["d2"], valid_at="2026-06-01")
    tmp_facts.supersede(fid, same, at="2026-06-01")
    row = tmp_facts.get(fid)
    assert row["status"] == "active"            # 仍 active,没丢
    assert row["support_count"] == 2            # 来源合并
    assert len(tmp_facts.query(canonical_id="SH600000")) == 1


def test_revive_after_superseded(tmp_facts):
    """旧事实被真替代后,同 key 事实再来 → 复活为 active。"""
    old = Fact(subject="A", predicate="HAS_ORDER_INTENT", object="定点",
               canonical_id="SH600000", claim="定点", sources=["d1"], valid_at="2026-01-01")
    fid = tmp_facts.upsert(old)
    newer = Fact(subject="A", predicate="HAS_CONFIRMED_ORDER", object="正式订单",
                 canonical_id="SH600000", claim="正式订单", sources=["d2"], valid_at="2026-02-01")
    tmp_facts.supersede(fid, newer, at="2026-02-01")
    assert tmp_facts.get(fid)["status"] == "superseded"
    # 同 key 定点事实再次出现 → 复活
    again = Fact(subject="A", predicate="HAS_ORDER_INTENT", object="定点",
                 canonical_id="SH600000", claim="定点", sources=["d3"], valid_at="2026-03-01")
    tmp_facts.upsert(again)
    assert tmp_facts.get(fid)["status"] == "active"


# ── C2:状态机接入摄入管线 ──────────────────────────────────────────────
@pytest.fixture
def stack(tmp_path):
    reg = EntityRegistry(tmp_path / "e.db")
    facts = FactsStore(tmp_path / "f.db")
    st = StructureStore(tmp_path / "s.db")
    ing = ResearchIngestor(reg, facts, st)
    yield reg, facts, st, ing
    reg.close(); facts.close(); st.close()


def test_pipeline_auto_supersede_progression(stack):
    """摄入"传闻"再摄入"确认订单"(同客户)→ 旧传闻被自动 supersede。"""
    reg, facts, st, ing = stack
    rep = IngestReport()
    card1 = {"id": "c1", "type": "industry", "date": "2026-05-01",
             "entities": [{"name": "绿的谐波", "kind": "stock", "code": "688017"}],
             "findings": [{"claim": "传闻绿的谐波获特斯拉减速器订单",
                           "entities": ["绿的谐波"], "numbers": [{"value": "1", "page": 1}]}]}
    card2 = {"id": "c2", "type": "industry", "date": "2026-06-01",
             "entities": [{"name": "绿的谐波", "kind": "stock", "code": "688017"}],
             "findings": [{"claim": "绿的谐波中标特斯拉减速器订单",
                           "entities": ["绿的谐波"], "numbers": [{"value": "1", "page": 1}]}]}
    ing.ingest_card(card1, rep)
    ing.ingest_card(card2, rep)
    active = facts.query(canonical_id="SH688017")
    preds = {f["predicate"] for f in active}
    assert "HAS_CONFIRMED_ORDER" in preds
    # 传闻应已被替代,不在 active
    allf = facts.query(canonical_id="SH688017", include_invalidated=True)
    assert any(f["predicate"] == "HAS_ORDER_RUMOR" and f["status"] == "superseded" for f in allf)


def test_object_overlap_guard():
    assert _object_overlap("特斯拉减速器订单", "中标特斯拉减速器") is True
    assert _object_overlap("特斯拉订单", "比亚迪电池产能") is False


# ── M1:市场代码路由 ────────────────────────────────────────────────────
def test_market_code_edge_cases():
    assert _to_market_code("688017") == "SH688017"
    assert _to_market_code("000001") == "SZ000001"
    assert _to_market_code("920819") == "BJ920819"   # 北交所 920(不再错挂 SH)
    assert _to_market_code("900901") == "SH900901"   # 沪B
    assert _to_market_code("200011") == "SZ200011"   # 深B(不再错挂 SH)
    assert _to_market_code("830799") == "BJ830799"
    assert _to_market_code("510300") == "SH510300"   # 沪 ETF
    assert _to_market_code("159915") == "SZ159915"   # 深 ETF
    assert _to_market_code("530010.OF") == "fund:530010"  # 场外基金不当股票
    assert _to_market_code("123456").startswith("stock_pending")  # 未知段不静默错挂
    assert _to_market_code("110011").startswith("stock_pending")  # 11x 非ETF段→pending


# ── M2:ask 不再 ASCII 子串误配 ─────────────────────────────────────────
def test_ask_no_ascii_substring_false_match(stack):
    reg, facts, st, ing = stack
    reg.resolve("PE", type_="concept")               # 短 ASCII 别名
    engine = AskEngine(reg, facts, st)
    cid = engine._locate_entity("performance review of factors")
    assert cid is None                                # 不应把 'pe' 误配


def test_keyword_recall_no_space_chinese(stack):
    """B2:无空格中文多概念查询应能召回(gram 重叠,不依赖用户加空格)。"""
    reg, facts, st, ing = stack
    from trading_kb.models import Fact
    facts.upsert(Fact(subject="多空博弈因子", predicate="HAS_FACTOR_PERFORMANCE",
                      object="多空博弈因子全市场选股效果出色", canonical_id="concept:多空博弈因子",
                      claim="多空博弈因子全市场选股效果出色", sources=["d1"]))
    engine = AskEngine(reg, facts, st)
    res = engine.ask("多空因子选股效果")          # 无空格
    assert res.facts, "无空格中文查询应有召回"


def test_entity_path_merges_keyword(stack):
    """B1:命中实体时也并入关键词召回,不坍缩成单条。"""
    reg, facts, st, ing = stack
    from trading_kb.models import Fact
    reg.resolve("动量因子", type_="concept")
    # 1 条直接挂动量因子实体 + 多条 claim 含"动量"的其他事实
    cid = reg.resolve("动量因子", type_="concept")
    facts.upsert(Fact(subject="动量因子", predicate="P", object="动量因子表现",
                      canonical_id=cid, claim="动量因子表现好", sources=["d0"]))
    for i in range(5):
        facts.upsert(Fact(subject=f"X{i}", predicate="HAS_FACTOR_PERFORMANCE",
                          object=f"动量类因子{i}", canonical_id=f"concept:x{i}",
                          claim=f"动量相关因子{i}有效", sources=[f"d{i}"]))
    engine = AskEngine(reg, facts, st)
    res = engine.ask("动量 因子")
    assert len(res.facts) > 1, "实体命中不应坍缩为单条"


def test_multi_entity_sentiment(tmp_path):
    """C5:一条碎片命中多个标的 → 各记一条。"""
    from trading_kb.sentiment_lane import SentimentLane
    reg = EntityRegistry(tmp_path / "e.db")
    sl = SentimentLane(tmp_path / "s.db", reg)
    sl.ingest_fragment("绿的谐波和宁德时代都要涨", "2026-06-10", ["绿的谐波", "宁德时代"])
    assert sl.stats()["items"] == 2
    sl.close(); reg.close()


def test_non_dict_card_skipped(tmp_path):
    """A1:cards 目录里非 dict 的 JSON 不击溃整批。"""
    import json
    from trading_kb.report_lab_adapter import iter_cards
    cd = tmp_path / "cards"
    cd.mkdir()
    (cd / "bad.json").write_text("[1,2,3]")        # list,非 dict
    (cd / "good.json").write_text(json.dumps({"id": "g", "type": "quant", "findings": []}))
    cards = list(iter_cards(cd))
    assert len(cards) == 1 and cards[0]["id"] == "g"


def test_structure_uses_card_entities(stack):
    """C3:finding 仅 1 实体时,从卡片级实体补第二端建结构边。"""
    reg, facts, st, ing = stack
    rep = IngestReport()
    card = {"id": "c1", "type": "industry", "date": "2026-05-01",
            "entities": [{"name": "谐波减速器", "kind": "concept"},
                         {"name": "人形机器人", "kind": "concept"}],
            "findings": [{"claim": "谐波减速器是人形机器人的上游核心部件",
                          "entities": ["谐波减速器"]}]}   # finding 只 1 实体
    ing.ingest_card(card, rep)
    assert rep.structures >= 1

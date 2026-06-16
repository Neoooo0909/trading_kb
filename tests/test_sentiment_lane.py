"""舆情轻 lane 测试:实体过滤、冷存留底、立场、聚合、隔离(D级)、升级。"""
import pytest

from trading_kb.entity_registry import EntityRegistry
from trading_kb.sentiment_lane import SentimentLane


@pytest.fixture
def lane(tmp_path):
    reg = EntityRegistry(tmp_path / "e.db")
    sl = SentimentLane(tmp_path / "sent.db", reg)
    yield sl, reg
    sl.close(); reg.close()


def test_entity_filter_keeps_only_watched(lane):
    sl, reg = lane
    watch = ["绿的谐波"]
    hit = sl.ingest_fragment("绿的谐波要起飞", "2026-06-10 09:30", watch)
    miss = sl.ingest_fragment("今天大盘随便聊聊", "2026-06-10 09:31", watch)
    assert hit is not None          # 命中关注标的 → 入库
    assert miss is None             # 未命中 → 冷存,不入库
    s = sl.stats()
    assert s["items"] == 1
    assert s["raw_total"] == 2      # 两条都冷存留底
    assert s["raw_kept"] == 1


def test_default_isolation_d_level(lane):
    sl, reg = lane
    item = sl.ingest_fragment("绿的谐波利好", "2026-06-10 10:00", ["绿的谐波"])
    assert item.evidence_level == "D"
    assert item.unverifiable is True
    assert item.promoted is False


def test_stance_detection(lane):
    sl, reg = lane
    bull = sl.ingest_fragment("绿的谐波要涨,利好", "t1", ["绿的谐波"])
    bear = sl.ingest_fragment("绿的谐波风险大,要跌", "t2", ["绿的谐波"])
    assert bull.stance == "bullish"
    assert bear.stance == "bearish"


def test_aggregate_signal(lane):
    sl, reg = lane
    for t, ts in [("绿的谐波要涨", "2026-06-10 09:00"),
                  ("绿的谐波利好加仓", "2026-06-10 10:00"),
                  ("绿的谐波要跌", "2026-06-11 09:00")]:
        sl.ingest_fragment(t, ts, ["绿的谐波"])
    cid = reg.resolve("绿的谐波", "stock")
    agg = sl.aggregate(cid)
    assert agg["total"] == 3
    assert agg["net_sentiment"] == 1          # 2 bullish - 1 bearish
    assert agg["density_by_day"]["2026-06-10"] == 2


def test_promote_gate(lane):
    sl, reg = lane
    sl.ingest_fragment("绿的谐波传闻定点", "t1", ["绿的谐波"])
    cid = reg.resolve("绿的谐波", "stock")
    n = sl.promote(cid, corroborating_source="docB")
    assert n == 1
    assert sl.stats()["promoted"] == 1

"""Web 控制台接口层测试:payload 转换在隔离数据目录下产出良构 JSON。"""
import trading_kb.config as config
from trading_kb import web
from trading_kb.entity_registry import EntityRegistry
from trading_kb.facts_store import FactsStore
from trading_kb.models import Fact


def _seed(tmp_path, monkeypatch):
    """把 config 全局库路径指到 tmp,并埋一条量化事实 + 一个股票实体。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ENTITY_DB", tmp_path / "e.db")
    monkeypatch.setattr(config, "FACTS_DB", tmp_path / "f.db")
    monkeypatch.setattr(config, "STRUCTURE_DB", tmp_path / "s.db")
    monkeypatch.setattr(config, "SENTIMENT_DB", tmp_path / "se.db")
    reg = EntityRegistry(config.ENTITY_DB)
    cid = reg.resolve("绿的谐波", type_="stock", stock_code="688017")
    reg.close()
    facts = FactsStore(config.FACTS_DB)
    facts.upsert(Fact(
        subject="绿的谐波", predicate="HAS_FACTOR_PERFORMANCE",
        object="测试因子年化收益40%", canonical_id=cid, claim="测试因子年化收益40%",
        evidence_level="B", unverifiable=False, source_kind="broker_research",
        sources=["doc1"], valid_at="2026-06-01", category="quant_fact",
    ))
    facts.close()
    return cid


def test_ask_payload_found(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = web.ask_payload("测试因子", audit=False)
    assert d["found"] is True
    assert d["conclusion"]["level"] == "B"
    assert any("测试因子" in e["claim"] for e in d["evidence"])
    # 结构良构:六段字段齐全
    for k in ("evidence", "doubts", "conflicts", "followup", "sources", "neighbors"):
        assert k in d


def test_ask_payload_not_found(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = web.ask_payload("完全不相关的查询zzz", audit=False)
    assert d["found"] is False
    assert d["conclusion"] is None


def test_stats_payload(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = web.stats_payload()
    assert d["facts"]["total"] >= 1
    assert d["entities"]["entities"] >= 1
    assert "structure" in d and "sentiment" in d


def test_feed_payload_explicit_watch(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = web.feed_payload("绿的谐波要起飞\n今天大盘没方向随便聊", "绿的谐波")
    assert d["ok"] is True
    assert d["kept"] == 1 and d["cold"] == 1


def test_feed_payload_default_pool(tmp_path, monkeypatch):
    """不传 watch → 用注册表已有股票池(绿的谐波)。"""
    _seed(tmp_path, monkeypatch)
    d = web.feed_payload("绿的谐波又有人传加单\n无关消息", "")
    assert d["ok"] is True
    assert d["kept"] == 1

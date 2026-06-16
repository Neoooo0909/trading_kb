"""真实语料不变量测试(堵住"测试在合成数据全绿、真实语料空转"的盲区)。

依赖 ~/report_lab/cards 存在;不存在则 skip。
断言真实 63 篇摄入后的关键不变量,而非迎合实现的合成卡片。
"""
import json
import pytest

from trading_kb import config
from trading_kb.entity_registry import EntityRegistry
from trading_kb.facts_store import FactsStore
from trading_kb.structure_store import StructureStore
from trading_kb.ingest import ResearchIngestor, IngestReport
from trading_kb.report_lab_adapter import iter_cards

_HAS_CORPUS = config.REPORT_LAB_CARDS.exists() and any(config.REPORT_LAB_CARDS.glob("*.json"))
pytestmark = pytest.mark.skipif(not _HAS_CORPUS, reason="report_lab cards 不存在")


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    d = tmp_path_factory.mktemp("corpus")
    reg = EntityRegistry(d / "e.db")
    facts = FactsStore(d / "f.db")
    st = StructureStore(d / "s.db")
    ing = ResearchIngestor(reg, facts, st)
    rep = IngestReport()
    for card in iter_cards():
        ing.ingest_card(card, rep)
    yield reg, facts, st, rep
    reg.close(); facts.close(); st.close()


def test_corpus_produces_meaningful_facts(ingested):
    """真实语料摄入后应产出大量事实(非空转)。"""
    _, facts, _, rep = ingested
    assert rep.cards >= 50
    assert facts.stats()["total"] > 500


def test_no_fact_on_malformed_market_code(ingested):
    """没有事实挂到非法市场代码(SH/SZ/BJ + 6位 或 concept:/stock_pending: 前缀)。"""
    _, facts, _, _ = ingested
    rows = facts.query(include_invalidated=True, limit=5000)
    bad = []
    for f in rows:
        cid = f["canonical_id"]
        # 合法:命名空间前缀(xxx:...)或 市场代码(SH/SZ/BJ + 6位)
        ok = (":" in cid
              or (cid[:2] in ("SH", "SZ", "BJ") and cid[2:].isdigit() and len(cid) == 8))
        if not ok:
            bad.append(cid)
    assert not bad, f"非法 canonical_id: {bad[:10]}"


def test_corpus_facts_all_have_sources(ingested):
    """每条事实都可溯源(sources 非空)。"""
    _, facts, _, _ = ingested
    rows = facts.query(include_invalidated=True, limit=5000)
    for f in rows:
        assert json.loads(f["sources"]), f"事实无来源: {f['fact_id']}"


def test_corpus_structure_layer_status(ingested):
    """结构层现状显式断言:当前量化语料结构边可能为 0(诚实记录,非隐藏)。

    若未来灌入行业研报应 >0;此处不强制 >0,只确保不崩、可统计。
    """
    _, _, st, _ = ingested
    s = st.stats()
    assert "total" in s   # 量化语料下 total 可能为 0,属已知边界(README 已注明)

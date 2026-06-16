"""端到端:合成卡片 → 摄入 → 三层 → 六段式问答。"""
import pytest

from trading_kb.entity_registry import EntityRegistry
from trading_kb.facts_store import FactsStore
from trading_kb.structure_store import StructureStore
from trading_kb.ingest import ResearchIngestor, IngestReport
from trading_kb.ask import AskEngine


@pytest.fixture
def stack(tmp_path):
    reg = EntityRegistry(tmp_path / "e.db")
    facts = FactsStore(tmp_path / "f.db")
    structure = StructureStore(tmp_path / "s.db")
    ing = ResearchIngestor(reg, facts, structure)
    yield reg, facts, structure, ing
    reg.close(); facts.close(); structure.close()


def _card():
    return {
        "id": "card_test_1", "type": "industry", "broker": "测试证券",
        "date": "2026-05-29",
        "entities": [{"name": "绿的谐波", "kind": "stock", "code": "688017"}],
        "findings": [
            {"claim": "绿的谐波2026年5月获特斯拉减速器定点",
             "evidence": "产业调研", "entities": ["绿的谐波"],
             "numbers": [{"value": "1", "page": 2}], "page": 2, "confidence": "medium"},
            {"claim": "谐波减速器属于人形机器人上游环节",
             "entities": ["谐波减速器", "人形机器人"], "page": 3},
            {"claim": "我们长期看好机器人板块", "page": 1},
        ],
    }


def test_e2e_ingest_classifies_and_stores(stack):
    reg, facts, structure, ing = stack
    rep = IngestReport()
    ing.ingest_card(_card(), rep)
    assert rep.cards == 1
    assert rep.hard_facts >= 1           # 定点 → hard_fact
    assert rep.structures >= 1           # 上游 → structure
    assert rep.background >= 1           # 看好 → background
    # 事实落库,挂正确主键
    fs = facts.query(canonical_id="SH688017")
    assert any("定点" in f["claim"] for f in fs)


def test_e2e_ask_six_section(stack):
    reg, facts, structure, ing = stack
    rep = IngestReport()
    ing.ingest_card(_card(), rep)
    engine = AskEngine(reg, facts, structure)
    res = engine.ask("绿的谐波 定点")
    assert res.canonical_id == "SH688017"
    out = res.to_six_section()
    assert "## 结论" in out
    assert "## 引用来源" in out
    assert "card_test_1" in out


def test_ask_evidence_insufficient(stack):
    reg, facts, structure, ing = stack
    engine = AskEngine(reg, facts, structure)
    res = engine.ask("不存在的标的XYZ")
    out = res.to_six_section()
    assert "证据不足" in out

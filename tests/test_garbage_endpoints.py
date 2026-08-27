"""结构边端点闸门(2026-08-27):is_garbage_entity 命中的名字不建 relations(此前造出 135 条端点不存在的孤儿边)。"""
import pytest

from trading_kb.entity_quality import is_garbage_entity
from trading_kb.entity_registry import EntityRegistry
from trading_kb.facts_store import FactsStore
from trading_kb.ingest import IngestReport, ResearchIngestor
from trading_kb.models import Finding
from trading_kb.structure_store import StructureStore


def _ing(tmp_path):
    return ResearchIngestor(EntityRegistry(tmp_path / "e.db"), FactsStore(tmp_path / "f.db"),
                            StructureStore(tmp_path / "s.db"))


def _sf(ents, claim="A 是 B 的供应商", doc="d1"):
    return Finding(claim=claim, entities=ents, numbers=[], doc_id=doc,
                   source_kind="broker_research", source_date="2026-08-01")


@pytest.mark.parametrize("bad", ["海外市场", "美伊停火", "上游", "公司"])
def test_garbage_names_are_recognised(bad):
    assert is_garbage_entity(bad, "concept"), bad


def test_two_garbage_ends_no_edge(tmp_path):
    ing = _ing(tmp_path); r = IngestReport()
    ing._ingest_structure(_sf(["海外市场", "美伊停火"]), r, [])
    assert r.structures == 0
    assert ing.structure.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def test_one_garbage_end_falls_back_no_edge(tmp_path):
    ing = _ing(tmp_path); r = IngestReport()
    ing._ingest_structure(_sf(["宁德时代", "海外市场"]), r, [])
    assert r.structures == 0
    assert ing.structure.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
    assert r.views + r.background == 1          # 走 view/留痕,不丢


def test_real_ends_still_build_edge(tmp_path):
    ing = _ing(tmp_path); r = IngestReport()
    ing._ingest_structure(_sf(["宁德时代", "特斯拉"]), r, [])
    assert r.structures == 1
    assert ing.structure.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1

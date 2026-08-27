"""(doc, claim) 判重索引(2026-08-27,BACKGROUND_FIX_PLAN §6.2):同一文档同一句只能有一行。"""
import importlib.util
import json
from pathlib import Path

from trading_kb.facts_store import FactsStore
from trading_kb.ingest import IngestReport, ResearchIngestor
from trading_kb.models import Fact, Finding

DEDUP = Path(__file__).resolve().parent.parent / "scripts" / "dedup_same_claim.py"


def _fact(doc, claim, cid="company:X", pred="HAS_VIEW", obj=None, level="C", **kw):
    return Fact(subject=cid.split(":")[-1], predicate=pred, object=obj or claim[:80], canonical_id=cid,
                claim=claim, evidence_level=level, source_kind="broker_research", sources=[doc],
                category=kw.pop("category", "view"), **kw)


def test_same_doc_same_claim_different_id_is_skipped(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    a = fs.upsert(_fact("d1", "公司订单饱满"))
    assert fs.last_upsert_dup is False
    # 主体口径演进(concept→company)→ fact_id 不同,但同文同句 → 不再插新行,返回旧 id
    b = fs.upsert(_fact("d1", "公司订单饱满", cid="concept:订单"))
    assert b == a and fs.last_upsert_dup is True
    assert fs.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    # 另一文档同句 → 合并到同 dedup_key 行(多源印证),且新 doc 也登记
    c = fs.upsert(_fact("d2", "公司订单饱满"))
    assert c == a and fs.last_upsert_dup is False
    assert json.loads(fs.get(a)["sources"]) == ["d1", "d2"]
    assert fs.doc_claim_find(["d2"], "公司订单饱满")["fact_id"] == a
    assert fs.doc_claim_status() == {"rows": 2, "dangling": 0}


def test_multi_stock_split_is_allowed(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    a = fs.upsert(_fact("d1", "A 与 B 达成合作", cid="SH600000"))
    b = fs.upsert(_fact("d1", "A 与 B 达成合作", cid="SZ000001"))
    assert a != b and fs.last_upsert_dup is False
    # 同一证券再来一次(id 口径变)仍拦
    c = fs.upsert(_fact("d1", "A 与 B 达成合作", cid="SH600000", pred="HAS_FORECAST"))
    assert c == a and fs.last_upsert_dup is True


def test_superseded_row_counts_as_existing_no_revive(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    old = fs.upsert(_fact("d1", "预计 2025 年投产", cid="SH600000", pred="HAS_FORECAST", category="hard_fact"))
    new = fs.supersede(old, _fact("d2", "预计 2026 年投产", cid="SH600000", pred="HAS_FORECAST", category="hard_fact"), at="2026-01-01")
    assert fs.get(old)["status"] == "superseded"
    again = fs.upsert(_fact("d1", "预计 2025 年投产", cid="concept:投产", pred="HAS_FORECAST", category="hard_fact"))
    assert again == old and fs.last_upsert_dup is True
    assert fs.get(old)["status"] == "superseded"          # 不复活
    assert new != old


def test_entities_union_on_hit(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    a = fs.upsert(_fact("d1", "行业景气回升", extra={"entities": ["甲"]}))
    fs.upsert(_fact("d1", "行业景气回升", cid="concept:景气", extra={"entities": ["乙"]}))
    ents = json.loads(fs.get(a)["extra"])["entities"]
    assert set(ents) == {"甲", "乙"}


def test_ingester_reports_dup_skipped(tmp_path):
    from trading_kb.entity_registry import EntityRegistry
    from trading_kb.structure_store import StructureStore
    reg = EntityRegistry(tmp_path / "e.db"); fs = FactsStore(tmp_path / "f.db"); st = StructureStore(tmp_path / "s.db")
    ing = ResearchIngestor(reg, fs, st)
    f = Finding(claim="精智达 2025 年营收 8 亿元", entities=["精智达"], numbers=[{"value": "8亿元"}],
                doc_id="card1", source_kind="broker_research", source_date="2026-01-01")
    r1 = IngestReport(); ing.ingest_finding(f, r1)
    r2 = IngestReport(); ing.ingest_finding(f, r2)
    assert r1.hard_facts == 1 and r2.hard_facts == 0 and r2.dup_skipped == 1
    assert fs.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


def test_build_and_remap(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    a = fs.upsert(_fact("d1", "句一")); b = fs.upsert(_fact("d2", "句二"))
    fs.conn.execute("DELETE FROM doc_claim"); fs.conn.commit()
    assert fs.doc_claim_status()["rows"] == 0
    r = fs.doc_claim_build()
    assert r["inserted"] == 2 and fs.doc_claim_status() == {"rows": 2, "dangling": 0}
    assert fs.doc_claim_remap(a, "newid") == 1
    assert fs.doc_claim_status()["dangling"] == 1
    fs.doc_claim_remap("newid", a)
    # dedup 合并后 loser 的登记应改指 keeper
    spec = importlib.util.spec_from_file_location("dedup_same_claim", DEDUP)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m._ensure_archive(fs.conn)
    # 造一对同文同句(绕过判重直接插)
    fs.conn.execute("INSERT INTO facts(fact_id,dedup_key,subject,predicate,object,canonical_id,claim,status,"
                    "evidence_level,unverifiable,source_kind,support_count,sources,valid_at,supersedes,category,extra)"
                    " VALUES('loser1','k1','X','HAS_VIEW','句一','concept:X','句一','active','C',1,'broker_research',1,"
                    "'[\"d1\"]','','[]','view','{}')")
    fs.conn.execute("INSERT OR REPLACE INTO doc_claim(doc_id,ckey,fact_id) VALUES('d1',?, 'loser1')", (fs.claim_key("句一"),))
    fs.conn.commit()
    keeper = dict(fs.conn.execute("SELECT * FROM facts WHERE fact_id=?", (a,)).fetchone())
    loser = dict(fs.conn.execute("SELECT * FROM facts WHERE fact_id='loser1'").fetchone())
    assert m.apply_group(fs, keeper, [loser], "2026-08-27T00:00:00") == "merged"
    assert fs.conn.execute("SELECT fact_id FROM doc_claim WHERE doc_id='d1'").fetchone()[0] == a
    assert fs.doc_claim_status()["dangling"] == 0

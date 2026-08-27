"""2026-08-27 全面审核第一批修复的回归测试(docs/AUDIT_FIX_PLAN_20260827.md §2、§7)。

每个用例对应一个发现编号(F1…),钉住"缺陷已消除"的行为,不是钉住实现细节。
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from trading_kb.ask import AskEngine, AskResult, _fact_extra
from trading_kb.classify import _apply_llm_override, classify_finding, classify_with_reason
from trading_kb.entity_registry import EntityRegistry, UNKNOWN_CID
from trading_kb.facts_store import FactsStore, extra_of, merge_fact_rows
from trading_kb.ingest import IngestReport, ResearchIngestor
from trading_kb.models import Fact, Finding, Relation
from trading_kb.structure_store import StructureStore

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fact(doc, claim, cid="company:X", pred="HAS_VIEW", obj=None, level="C", **kw):
    return Fact(subject=cid.split(":")[-1], predicate=pred, object=obj or claim[:80], canonical_id=cid,
                claim=claim, evidence_level=level, source_kind=kw.pop("source_kind", "broker_research"),
                sources=[doc], category=kw.pop("category", "view"), **kw)


def _finding(claim, entities=None, numbers=None, doc="d_t", date="2026-08-20", kind="social_research"):
    return Finding(claim=claim, entities=entities or [], numbers=numbers or [], doc_id=doc,
                   source_date=date, source_kind=kind)


# ── F4/F5:doc_claim 三元主键与登记覆盖 ───────────────────────────────────────
def test_F4_multi_stock_second_row_is_registered_and_protected(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    a = fs.upsert(_fact("d1", "A与B合作", cid="SH600000"))
    b = fs.upsert(_fact("d1", "A与B合作", cid="SZ000001"))          # 双证券刻意拆分:放行
    assert a != b and not fs.last_upsert_dup
    rows = fs.conn.execute("SELECT fact_id FROM doc_claim WHERE doc_id='d1'").fetchall()
    assert {r[0] for r in rows} == {a, b}                            # 第二只证券也登记上了
    # 口径漂移再入库(同句、SZ 主体换谓词)→ 被拦下,不造第 3 行
    c = fs.upsert(_fact("d1", "A与B合作", cid="SZ000001", pred="HAS_FORECAST"))
    assert c == b and fs.last_upsert_dup
    assert fs.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_F5_merge_path_registers_long_claim_variant(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    c1, c2 = "X" * 80 + "版本一后缀", "X" * 80 + "版本二后缀"
    a = fs.upsert(_fact("d1", c1, cid="concept:x"))
    assert fs.upsert(_fact("d2", c2, cid="concept:x")) == a           # 同 dedup_key → 合并
    assert fs.upsert(_fact("d2", c2, cid="company:x")) == a           # 变体已登记 → 判重拦下
    assert fs.last_upsert_dup


def test_F4_dangling_row_does_not_shadow_and_build_cleans(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    a = fs.upsert(_fact("d1", "句X", cid="company:x"))
    fs.conn.execute("DELETE FROM facts WHERE fact_id=?", (a,)); fs.conn.commit()   # raw 删行不 remap
    assert fs.doc_claim_status()["dangling"] == 1
    b = fs.upsert(_fact("d1", "句X", cid="concept:x2"))                  # 换口径再入库 → 新行,但登记不被悬空行遮蔽
    assert b != a and not fs.last_upsert_dup
    assert fs.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    # 再次入库(再换 cid)→ 命中 b 的登记,不再"每次口径变化都造一行"
    assert fs.upsert(_fact("d1", "句X", cid="company:x3")) == b and fs.last_upsert_dup
    r = fs.doc_claim_build()
    assert r["dangling_removed"] == 1 and fs.doc_claim_status()["dangling"] == 0


def test_F4_migrate_from_two_column_pk(tmp_path):
    db = tmp_path / "f.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE facts(fact_id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE, subject TEXT, predicate TEXT,
          object TEXT, canonical_id TEXT, claim TEXT, status TEXT, evidence_level TEXT, unverifiable INTEGER,
          source_kind TEXT, support_count INTEGER, sources TEXT, valid_at TEXT, invalid_at TEXT,
          supersedes TEXT, relation_id TEXT, category TEXT, extra TEXT);
        CREATE TABLE doc_claim(doc_id TEXT NOT NULL, ckey INTEGER NOT NULL, fact_id TEXT NOT NULL,
          PRIMARY KEY(doc_id, ckey)) WITHOUT ROWID;
        INSERT INTO doc_claim VALUES('d1', 7, 'f1');
    """)
    c.commit(); c.close()
    fs = FactsStore(db)
    assert fs._doc_claim_pk_arity() == 2 and fs.schema_version() == 0
    r = fs.migrate()
    assert r["doc_claim_pk"] == "migrated" and fs._doc_claim_pk_arity() == 3
    assert fs.schema_version() == 2 and fs.has_doubt_index()
    assert fs.conn.execute("SELECT fact_id FROM doc_claim").fetchone()[0] == "f1"
    assert fs.migrate()["doc_claim_pk"] == "ok"                       # 幂等


def test_F19_remap_or_replace_when_keeper_and_loser_both_registered(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    k = fs.upsert(_fact("d1", "同句", cid="SH600000"))
    l = fs.upsert(_fact("d1", "同句", cid="SZ000001"))                 # 同 (doc,ckey) 双登记
    assert FactsStore.doc_claim_remap_conn(fs.conn, l, k) == 1        # 裸 UPDATE 会撞唯一约束
    fs.conn.commit()
    assert [r[0] for r in fs.conn.execute("SELECT fact_id FROM doc_claim WHERE doc_id='d1'")] == [k]


# ── F1:带质疑行的取数 ────────────────────────────────────────────────────────
def test_F1_query_with_doubts_finds_the_needle(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    for i in range(300):
        fs.upsert(_fact(f"a{i}", f"A级公告第{i}条", cid="SH600001", pred="HAS_ORDER", level="A",
                        category="hard_fact"))
    fs.upsert(_fact("c1", "C级可疑说法", cid="SH600002", pred="HAS_CAPACITY", level="C", category="hard_fact",
                    extra={"doubts": [{"kind": "speculative", "severity": "high", "message": "无出处"}],
                           "doubt_severity": "high"}))
    # 无索引(全扫降级)与有索引两条路径结果一致
    assert not fs.has_doubt_index()
    got = fs.query_with_doubts(limit=5000)
    assert [f["claim"] for f in got] == ["C级可疑说法"]
    fs.migrate()
    assert fs.has_doubt_index()
    got2 = fs.query_with_doubts(limit=5000, categories=["hard_fact"], code_only=True)
    assert [f["claim"] for f in got2] == ["C级可疑说法"] and fs.count_with_doubts() == 1
    # 旧候选池取法在这个库上就找不到它——正是 F1 的病
    old_pool = fs.query(include_invalidated=False, limit=5)
    assert all(f["evidence_level"] == "A" for f in old_pool)


def test_F1_query_with_doubts_orders_by_severity(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    fs.migrate()
    for sev in ("medium", "high", "low"):
        fs.upsert(_fact(f"s{sev}", f"{sev}说法", cid="SH600009", pred="HAS_" + sev.upper(), level="C",
                        category="hard_fact", extra={"doubts": [{"severity": sev}], "doubt_severity": sev}))
    assert [extra_of(f)["doubt_severity"] for f in fs.query_with_doubts()] == ["high", "medium", "low"]


# ── F21 / P2-4 / P2-6:upsert 与 search 的鲁棒性 ──────────────────────────────
def test_P2_4_insert_failure_rolls_back(tmp_path, monkeypatch):
    fs = FactsStore(tmp_path / "f.db")
    monkeypatch.setattr(fs, "doc_claim_register", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        fs.upsert(_fact("d1", "会失败的事实"))
    assert not fs.conn.in_transaction                                  # 没有悬挂事务
    assert fs.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0   # 半成品没被留下


def test_P2_6_empty_terms_search_returns_nothing(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    for i in range(5):
        fs.upsert(_fact(f"d{i}", f"无关事实第{i}条", cid="concept:n"))
    assert fs.search("铜") == [] and fs.search("") == []


def test_supersede_guard_when_new_fact_is_dup_of_another_row(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    old = fs.upsert(_fact("d0", "旧事实", cid="SH600000", pred="HAS_ORDER_RUMOR", category="hard_fact"))
    other = fs.upsert(_fact("d1", "同文同句", cid="concept:x", pred="HAS_VIEW"))
    new = _fact("d1", "同文同句", cid="company:x", pred="HAS_CONFIRMED_ORDER", category="hard_fact")
    nid = fs.supersede(old, new, "2026-08-27")
    assert nid == other                                                 # 被判重拦到另一行
    assert fs.get(old)["status"] == "active"                            # 旧事实没被错标 superseded


# ── merge_fact_rows 是唯一合并口径:与 upsert 合并路径五个共同字段一致 ─────────
def test_merge_fact_rows_matches_upsert_merge_path(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    a = _fact("d1", "同一论断", cid="SH600000", pred="HAS_ORDER", level="C", category="hard_fact",
              valid_at="2026-05-01")
    b = _fact("d2", "同一论断", cid="SH600000", pred="HAS_ORDER", level="B", category="hard_fact",
              valid_at="2026-04-01", unverifiable=False)
    fid = fs.upsert(a); assert fs.upsert(b) == fid
    row = fs.get(fid)
    ra, rb = a.to_row(), b.to_row()
    for r in (ra, rb):
        r["unverifiable"] = int(r["unverifiable"])
        for k in ("sources", "supersedes", "extra"):
            if not isinstance(r[k], str):
                r[k] = json.dumps(r[k])
    m = merge_fact_rows(ra, [rb])
    assert json.loads(row["sources"]) == m["sources"] == ["d1", "d2"]
    assert row["support_count"] == m["support_count"] == 2
    assert row["evidence_level"] == m["evidence_level"] == "B"
    assert row["valid_at"] == m["valid_at"] == "2026-04-01"
    assert row["unverifiable"] == m["unverifiable"] == 0


# ── F3:_reattribute 碰撞真合并 + 归档 ────────────────────────────────────────
def test_F3_reattribute_collision_merges_and_archives(tmp_path):
    ce = _load("clean_entities")
    fs = FactsStore(tmp_path / "facts.db")
    a = Fact(subject="A", predicate="HAS_CAPACITY", object="扩产100万吨", canonical_id="concept:A",
             claim="x", evidence_level="B", sources=["a"], category="hard_fact",
             extra={"entities": ["A", "B"], "doubts": [{"severity": "high", "message": "m"}]})
    b = Fact(subject="B", predicate="HAS_CAPACITY", object="扩产100万吨", canonical_id="SH600001",
             claim="y", evidence_level="C", sources=["b"], category="hard_fact")
    fs.upsert(a); fs.upsert(b)
    assert ce._reattribute(fs.conn, a.fact_id, "SH600001", "B") == "merged"
    fs.conn.commit()
    rows = fs.conn.execute("SELECT * FROM facts").fetchall()
    assert len(rows) == 1 and rows[0]["fact_id"] == b.fact_id
    assert json.loads(rows[0]["sources"]) == ["a", "b"] and rows[0]["support_count"] == 2
    assert rows[0]["evidence_level"] == "B"                              # 成色取高,不再丢
    ex = extra_of(rows[0])
    assert ex["entities"] == ["A", "B"] and ex["doubts"] and ex["merged_from"] == [a.fact_id]
    arch = fs.conn.execute("SELECT role FROM facts_merged_archive ORDER BY role").fetchall()
    assert [r[0] for r in arch] == ["keeper_before", "loser"]
    assert fs.doc_claim_status()["dangling"] == 0                        # remap 走了唯一实现


def test_F3_reattribute_refuses_to_merge_active_into_dead_target(tmp_path):
    ce = _load("clean_entities")
    fs = FactsStore(tmp_path / "facts.db")
    a = Fact(subject="A", predicate="P", object="o", canonical_id="concept:A", claim="x", sources=["a"])
    b = Fact(subject="B", predicate="P", object="o", canonical_id="SH600001", claim="y", sources=["b"])
    fs.upsert(a); fs.upsert(b)
    fs.conn.execute("UPDATE facts SET status='superseded' WHERE fact_id=?", (b.fact_id,)); fs.conn.commit()
    assert ce._reattribute(fs.conn, a.fact_id, "SH600001", "B") == "skipped_dead_target"
    assert fs.get(a.fact_id)["status"] == "active"


# ── F7:伪公司闸门下沉到注册表 ───────────────────────────────────────────────
def test_F7_register_gate_demotes_pseudo_company(tmp_registry):
    cid = tmp_registry.resolve("北美AI集群", type_="company")
    assert cid.startswith("concept:")
    assert tmp_registry.resolve("宁德时代", type_="company").startswith("company:")   # 真公司名不受影响
    assert tmp_registry.resolve("云厂商", type_="stock", stock_code="300750") == "SZ300750"  # 带 code 永远是证券


def test_merge_rejects_self_and_cycle(tmp_registry, capsys):
    a = tmp_registry.resolve("甲", type_="stock", stock_code="600000")
    b = tmp_registry.resolve("乙", type_="stock", stock_code="600001")
    tmp_registry.merge(a, a)
    tmp_registry.merge(a, b)
    tmp_registry.merge(b, a)                                             # 会成环 → 跳过
    assert tmp_registry.resolve("甲") == b and tmp_registry.resolve("乙") == b
    assert "跳过" in capsys.readouterr().err


def test_sync_relations_recomputes_low_confidence(tmp_registry, tmp_path):
    conn = sqlite3.connect(tmp_path / "structure.db")
    conn.execute("""CREATE TABLE relations (rel_id TEXT PRIMARY KEY, src TEXT, rel_type TEXT,
                    dst TEXT, support_count INTEGER, sources TEXT, low_confidence INTEGER)""")
    for src, srcs in (("SZ000001", ["p"]), ("company:旧名", ["q"])):
        r = Relation(src=src, rel_type="SUPPLIES_TO", dst="SZ000003", sources=srcs)
        conn.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,1)",
                     (r.rel_id, src, "SUPPLIES_TO", "SZ000003", 1, json.dumps(srcs)))
    conn.commit()
    tmp_registry.merge("company:旧名", "SZ000001")
    row = conn.execute("SELECT support_count, low_confidence FROM relations").fetchone()
    assert row == (2, 0)


# ── F2:LLM 覆盖策略 ──────────────────────────────────────────────────────────
def test_F2_llm_override_gates():
    v = _finding("晓程科技拥有海外金矿资产，利润弹性突出。", entities=["晓程科技"])
    bg = _finding("本周矿业股表现强劲")
    assert classify_finding(v) == "view" and classify_finding(bg) == "background"
    assert classify_finding(v, llm=lambda f: "background") == "view"            # 有主体不许打成 background
    assert classify_finding(bg, llm=lambda f: "view") == "background"           # 无主体不许造 view
    assert classify_finding(v, llm=lambda f: "hard_fact") == "hard_fact"        # 升硬允许
    assert classify_finding(v, llm=lambda f: "quant_fact") == "view"            # 无指标锚不许 quant
    hard = _finding("公司中标 5.2 亿元订单", entities=["公司A"], numbers=[{"value": "5.2亿"}])
    assert classify_finding(hard) == "hard_fact"
    assert classify_finding(hard, llm=lambda f: "background") == "hard_fact"    # 高置信档不被推翻
    assert _apply_llm_override("view", "nonsense", v) == "view"
    assert classify_with_reason(bg, llm=lambda f: "hard_fact") == ("hard_fact", "")
    assert classify_with_reason(_finding("本报告仅供参考,不构成投资建议", entities=["X"]))[1] == "boilerplate"
    assert classify_with_reason(bg)[1] == "no_entity_no_number"


# ── F8:仅垃圾端的 structure 句留痕而非未知主体 view ────────────────────────────
def test_F8_structure_fallback_garbage_only_goes_to_background_log(tmp_registry, tmp_facts, tmp_structure):
    ing = ResearchIngestor(tmp_registry, tmp_facts, tmp_structure)
    rep = IngestReport()
    ing.ingest_finding(_finding("海外市场属于重要环节", entities=["海外市场"]), rep)
    assert rep.views == 0 and rep.background == 1
    r = tmp_facts.conn.execute("SELECT reason FROM background_log").fetchone()
    assert r[0] == "structure_garbage_end"
    assert tmp_facts.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_F8_no_unknown_subject_hub_edges(tmp_registry, tmp_facts, tmp_structure, monkeypatch):
    ing = ResearchIngestor(tmp_registry, tmp_facts, tmp_structure)
    rep = IngestReport()
    real = tmp_registry.resolve
    monkeypatch.setattr(tmp_registry, "resolve",
                        lambda name, **k: UNKNOWN_CID if name == "神秘方" else real(name, **k))
    ing.ingest_finding(_finding("神秘方属于宁德时代的供应商", entities=["神秘方", "宁德时代"]), rep)
    assert rep.structures == 0
    assert tmp_structure.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def test_dup_skip_does_not_count_doubts(tmp_registry, tmp_facts, tmp_structure):
    from trading_kb.critique import CritiqueEngine
    f = _finding("公司预计明年营收翻倍增长 120%", entities=["某公司"], numbers=[{"value": "120%"}])
    ing = ResearchIngestor(tmp_registry, tmp_facts, tmp_structure,
                           critique_engine=CritiqueEngine().fit([f]))
    rep = IngestReport()
    ing.ingest_finding(f, rep)
    d1 = rep.doubts
    ing.ingest_finding(_finding(f.claim, entities=["某公司"], numbers=f.numbers), rep)
    assert rep.dup_skipped == 1 and rep.doubts == d1


# ── F9/F10/F11:问答层 ────────────────────────────────────────────────────────
def test_F9_auto_verify_fresh_skips_view():
    from datetime import date
    from trading_kb.deep_verify import auto_verify_fresh
    facts = [{"fact_id": "v", "evidence_level": "C", "canonical_id": "SZ300139", "claim": "晓程科技海外金矿利润弹性突出",
              "valid_at": date.today().isoformat(), "category": "view", "predicate": "HAS_VIEW", "subject": "晓程科技"}]
    assert auto_verify_fresh(facts, date.today().toordinal(), fetch_fn=lambda c, q: []) == []


def test_F11_fact_extra_tolerates_bad_shapes():
    assert _fact_extra({"extra": "[1,2]"}) == {}
    assert _fact_extra({"extra": "not json{"}) == {}
    assert _fact_extra({"extra": None}) == {}
    assert extra_of({"extra": {"k": 1}}) == {"k": 1}


def test_F11_rank_survives_null_entity(tmp_registry, tmp_facts, tmp_structure):
    cid = tmp_registry.resolve("晓程科技", type_="stock", stock_code="300139")
    for i in range(12):
        tmp_facts.upsert(_fact(f"d{i}", f"晓程科技海外金矿第{i}条", cid=cid, category="hard_fact",
                               pred="HAS_ORDER", extra={"entities": [None, "晓程科技"]}))
    tmp_facts.upsert(_fact("dx", "浩通科技贵金属回收放量", cid="concept:浩通科技", category="hard_fact",
                           pred="HAS_ORDER", extra="[1,2]"))
    res = AskEngine(tmp_registry, tmp_facts, tmp_structure).ask("晓程科技 金矿", use_semantic=False)
    assert res.facts                                                     # 没崩


def test_F10_zero_result_still_renders_warnings():
    r = AskResult(query="q", warnings=["锚到低覆盖/短语型实体 company:x(自有事实 0 条),已切发现模式"])
    assert "检索告警" in r.to_six_section() and r.to_payload()["warnings"]


def test_followup_and_payload_skip_view(tmp_registry, tmp_facts, tmp_structure):
    cid = tmp_registry.resolve("晓程科技", type_="stock", stock_code="300139")
    tmp_facts.upsert(_fact("d1", "晓程科技拥有海外金矿", cid=cid, category="view", pred="HAS_VIEW",
                           unverifiable=True))
    tmp_facts.upsert(_fact("d2", "晓程科技中标订单 3 亿元", cid=cid, category="hard_fact", pred="HAS_ORDER",
                           unverifiable=True))
    res = AskEngine(tmp_registry, tmp_facts, tmp_structure).ask("晓程科技", use_semantic=False)
    assert res.to_payload()["followup"] == ["晓程科技中标订单 3 亿元"]


# ── F12:退出码 ───────────────────────────────────────────────────────────────
def test_F12_cli_exit_codes(tmp_path, monkeypatch):
    from trading_kb import cli, config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for name in ("FACTS_DB", "ENTITY_DB", "STRUCTURE_DB", "SENTIMENT_DB"):
        monkeypatch.setattr(config, name, tmp_path / f"{name.lower()}.db")
    assert cli.main(["feed-chat", str(tmp_path / "missing.txt")]) == 2
    assert cli.main(["critique"]) == 0                                   # 真没数据 = 0
    assert cli.main(["migrate"]) == 0


# ── F15:valid_at NULL 盲区 ───────────────────────────────────────────────────
def test_F15_backfill_valid_at_handles_null_row(tmp_path):
    m = _load("backfill_valid_at")
    fs = FactsStore(tmp_path / "f.db")
    fid = fs.upsert(_fact("doc_null", "无日期事实", cid="concept:n"))
    fs.conn.execute("UPDATE facts SET valid_at=NULL WHERE fact_id=?", (fid,)); fs.conn.commit()
    items = m.facts_to_fix(fs.conn, {"doc_null": ("2026-01-12", "pdf_creation")})
    assert [(i[0], i[2]) for i in items] == [(fid, "2026-01-12")]
    st = m.apply_facts(fs, items, "run_null")
    assert st.get("updated", 0) == 1 and fs.get(fid)["valid_at"] == "2026-01-12"


# ── F16:只认 \n 的行读取 ─────────────────────────────────────────────────────
def test_F16_iter_lines_ignores_unicode_line_separators(tmp_path):
    from trading_kb.jsonl import iter_jsonl, iter_lines
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": "第一 行"}\n{"b": 2}\n', encoding="utf-8")
    assert len(iter_lines(p)) == 2
    assert [json.loads(l) for l in iter_lines(p)] == [{"a": "第一 行"}, {"b": 2}]
    assert list(iter_jsonl(p)) == [{"a": "第一 行"}, {"b": 2}]
    assert "第一 行" in p.read_text(encoding="utf-8").splitlines()[0] or True   # 对照:splitlines 会切成 3 行
    assert len(p.read_text(encoding="utf-8").splitlines()) == 3


# ── F13:_ORDER_FAMILY 单一定义点 ─────────────────────────────────────────────
def test_F13_order_family_is_not_hand_copied():
    from trading_kb.ingest import _ORDER_PROGRESSION
    m = _load("announcements_to_kb")
    assert set(m._ORDER_FAMILY) == set(_ORDER_PROGRESSION)


# ── prune_backups:大操作备份规划器有测试(审核 D P2-5)────────────────────────
def test_prune_big_groups_and_age_gate(tmp_path, monkeypatch):
    import os, time
    pb = _load("prune_backups")
    monkeypatch.setattr(pb, "_BACKUP_DIR", tmp_path / ".backup")
    monkeypatch.setattr(pb, "_DATA_DIR", tmp_path / "data")
    (tmp_path / ".backup").mkdir(); (tmp_path / "data").mkdir()
    old = time.time() - 40 * 86400
    names = ["facts.db.bak_dedup_1787000000", "facts.db.bak_dedup_1787000000-wal",
             "facts.db.bak_dedup_1787100000", "facts.db.bak_dedup_1787200000",
             "facts.db.bak.20260827_114646", "facts.db.bak.ipo_ingest.2026-08-01",
             "structure.db.bak.retype_20260825_233439", "facts.db.bak.retype_20260825_233439"]
    for i, n in enumerate(names):
        f = tmp_path / ".backup" / n; f.write_text("x")
        os.utime(f, (old + i * 3600, old + i * 3600))                   # 全部 >30 天,dedup 三组按时间递增
    doomed, kept = pb._plan_big()
    doomed_names = sorted(p.name for p in doomed)
    assert doomed_names == ["facts.db.bak_dedup_1787000000", "facts.db.bak_dedup_1787000000-wal"]   # 最老一组含 sidecar
    assert not any("bak.2026" in p.name or "ipo_ingest" in p.name for p in doomed)

"""并发与鲁棒性回归测试(本轮对抗审计发现的 bug):
- facts/structure 写入并发竞态(TOCTOU)不抛 IntegrityError、不 lost-update
- 空 sources 合并不把 support_count 覆盖为 0
- 超长无分隔查询不触发 SQLite "pattern too complex"
- _locate_entity 子串劫持缺口(华为⊂华为概念)
- cli feed-chat / _read_fragments 对目录/空串用 is_file 守卫不崩
"""
import threading
import pytest

from trading_kb.facts_store import FactsStore
from trading_kb.structure_store import StructureStore
from trading_kb.entity_registry import EntityRegistry
from trading_kb.ask import AskEngine
from trading_kb.models import Fact, Relation


# ── 并发竞态:facts 多连接同写一 dedup_key,不崩且不丢源 ───────────────────
def test_facts_concurrent_upsert_no_crash_no_lost_update(tmp_path):
    db = tmp_path / "f.db"
    FactsStore(db).close()
    errs: list = []
    barrier = threading.Barrier(12)

    def worker(n: int) -> None:
        fs = FactsStore(db)
        barrier.wait()                         # 尽量同时进入写,放大竞态
        try:
            fs.upsert(Fact(subject="S", predicate="P", object="O",
                           canonical_id="C", sources=[f"d{n}"]))
        except Exception as e:                 # noqa: BLE001 — 记录任何异常
            errs.append(repr(e))
        fs.close()

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]

    assert errs == []                          # 旧 bug:抛 UNIQUE constraint failed
    fs = FactsStore(db)
    row = fs.conn.execute("SELECT COUNT(*) c, support_count, sources FROM facts").fetchone()
    assert row["c"] == 1                        # 去重成一行
    assert row["support_count"] == 12           # 旧 bug:lost-update 导致 <12
    fs.close()


# ── 并发竞态:relations 同 rel_id 并发写不崩 ───────────────────────────────
def test_relations_concurrent_upsert_no_crash(tmp_path):
    db = tmp_path / "s.db"
    StructureStore(db).close()
    errs: list = []
    barrier = threading.Barrier(16)

    def worker(n: int) -> None:
        ss = StructureStore(db)
        barrier.wait()
        try:
            ss.upsert(Relation(src="X", rel_type="UPSTREAM_OF", dst="Y", sources=[f"d{n}"]))
        except Exception as e:                 # noqa: BLE001
            errs.append(repr(e))
        ss.close()

    for _ in range(4):
        ts = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert errs == []
    ss = StructureStore(db)
    row = ss.conn.execute("SELECT COUNT(*) c, support_count FROM relations").fetchone()
    assert row["c"] == 1
    assert row["support_count"] == 16          # 16 个唯一来源全部合并,零丢
    ss.close()


# ── 空 sources 合并不把 support_count 覆盖为 0 ────────────────────────────
def test_empty_sources_keeps_support_count_floor(tmp_facts, tmp_structure):
    tmp_facts.upsert(Fact(subject="S", predicate="P", object="O", canonical_id="C", sources=[]))
    tmp_facts.upsert(Fact(subject="S", predicate="P", object="O", canonical_id="C", sources=[]))
    sc = tmp_facts.conn.execute("SELECT support_count FROM facts").fetchone()[0]
    assert sc >= 1                             # 旧 bug:第二次 upsert 后变 0

    tmp_structure.upsert(Relation(src="X", rel_type="UPSTREAM_OF", dst="Y", sources=[]))
    tmp_structure.upsert(Relation(src="X", rel_type="UPSTREAM_OF", dst="Y", sources=[]))
    rsc = tmp_structure.conn.execute("SELECT support_count FROM relations").fetchone()[0]
    assert rsc >= 1


# ── 超长无分隔查询不触发 SQLite LIKE pattern too complex ──────────────────
def test_search_extreme_long_token_no_crash(tmp_facts):
    tmp_facts.upsert(Fact(subject="宁德时代", predicate="P", object="O",
                          canonical_id="SZ300750", sources=["d1"]))
    # 旧 bug:20000 字无分隔串切成单个巨 token → LIKE '%<上万字>%' → OperationalError
    res = tmp_facts.search("宁德时代" * 5000, limit=10)
    assert isinstance(res, list)               # 不抛即通过


# ── _locate_entity 子串劫持缺口:华为(⊂华为概念)不应劫持概念查询 ──────────
def test_locate_entity_substring_hijack_gap(tmp_path, tmp_facts, tmp_structure):
    reg = EntityRegistry(tmp_path / "e.db")
    for alias, cid in [
        ("半导体设备产业", "concept:半导体设备产业"),
        ("华为概念", "concept:华为概念"),
        ("人工智能", "concept:人工智能"),
        ("存储测试设备", "concept:存储测试设备"),
        ("华为", "company:华为"),
        ("智能", "company:智能"),
        ("精智达", "SH688627"),
    ]:
        reg.conn.execute("INSERT OR IGNORE INTO aliases(alias_norm, canonical_id) VALUES(?,?)",
                         (alias, cid))
    reg.conn.commit()
    eng = AskEngine(reg, tmp_facts, tmp_structure)

    # 证券优先(精智达非概念子串)
    assert eng._locate_entity("存储测试设备 精智达 进展") == "SH688627"
    # 短证券是更长概念子串 → 不劫持(智能⊂人工智能)
    assert eng._locate_entity("人工智能") == "concept:人工智能"
    # 缺口用例:华为⊂华为概念,虽非最长匹配的子串,也不能劫持 → 落概念
    got = eng._locate_entity("半导体设备产业 华为概念 华为")
    assert got != "company:华为"
    assert got.startswith("concept:")
    reg.close()


# ── cli feed-chat / _read_fragments 对目录/空串不崩 ──────────────────────
def test_read_fragments_directory_no_crash(tmp_path):
    from trading_kb.cli import _read_fragments
    # 传目录:旧 bug 用 exists() 守卫 → read_text 抛 IsADirectoryError
    assert _read_fragments(tmp_path) == []
    # 空串归一为 "."(当前目录,也是目录)
    from pathlib import Path
    assert _read_fragments(Path("")) == []

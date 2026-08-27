"""实体注册表测试:归一、别名、合并、code 前缀。"""
from trading_kb.entity_registry import EntityRegistry, _to_market_code


def test_market_code_prefix():
    assert _to_market_code("688017") == "SH688017"
    assert _to_market_code("000001") == "SZ000001"
    assert _to_market_code("300750") == "SZ300750"
    assert _to_market_code("830799") == "BJ830799"
    assert _to_market_code("SH600519") == "SH600519"


def test_stock_resolves_by_code(tmp_registry):
    cid = tmp_registry.resolve("绿的谐波", type_="stock", stock_code="688017")
    assert cid == "SH688017"
    # 同名再解析,拿到同一主键
    assert tmp_registry.resolve("绿的谐波", type_="stock") == "SH688017"


def test_concept_canonical(tmp_registry):
    cid = tmp_registry.resolve("固态电池", type_="concept")
    assert cid.startswith("concept:")
    # 归一:大小写/空白无关
    assert tmp_registry.resolve(" 固态电池 ", type_="concept") == cid


def test_alias_and_merge(tmp_registry):
    cid = tmp_registry.resolve("宁德时代", type_="stock", stock_code="300750")
    tmp_registry.add_alias("CATL", cid)
    assert tmp_registry.resolve("CATL") == cid
    # 合并:把一个碎片实体并入主实体
    frag = tmp_registry.resolve("宁德", type_="stock")
    tmp_registry.merge(frag, cid)
    assert tmp_registry.resolve("宁德") == cid


def test_stats(tmp_registry):
    tmp_registry.resolve("A", "concept")
    tmp_registry.resolve("B", "stock")   # pending
    s = tmp_registry.stats()
    assert s["entities"] >= 2
    assert s["pending_stocks"] >= 1


# ── merge 同步关系边(2026-08-27)──────────────────────────────────────────
def _mk_structure(tmp_path):
    """在 registry 同目录建 structure.db(merge 靠 db_path.parent 找到它)。"""
    import sqlite3
    conn = sqlite3.connect(tmp_path / "structure.db")
    conn.execute("""CREATE TABLE relations (rel_id TEXT PRIMARY KEY, src TEXT, rel_type TEXT,
                    dst TEXT, support_count INTEGER, sources TEXT, low_confidence INTEGER)""")
    return conn


def _add_rel(conn, src, rel_type, dst, sources):
    import hashlib, json
    from trading_kb.models import _normalize
    key = f"{_normalize(src)}|{rel_type}|{_normalize(dst)}"
    rid = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    conn.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,0)",
                 (rid, src, rel_type, dst, len(sources), json.dumps(sources)))
    conn.commit()
    return rid


def test_merge_repoints_relations(tmp_registry, tmp_path):
    """merge 必须把 relations 的 src/dst 改指到合并终点。

    不改指的后果是【静默丢失】:边上留旧 cid,neighbors(新 cid) 查不到,
    而旧 cid 已被 resolve 解析成新 cid 也查不到——两头都够不着。
    """
    conn = _mk_structure(tmp_path)
    _add_rel(conn, "company:新易胜", "SUPPLIES_TO", "SZ300308", ["a"])
    tmp_registry.merge("company:新易胜", "SZ300502")
    row = conn.execute("SELECT src, dst FROM relations").fetchone()
    assert row == ("SZ300502", "SZ300308")
    conn.close()


def test_merge_relation_collision_unions_sources(tmp_registry, tmp_path):
    """改指后撞上已存在的同 rel_id 边 → 并入,sources 取并集、support 跟随。"""
    import json
    conn = _mk_structure(tmp_path)
    _add_rel(conn, "SZ300502", "SUPPLIES_TO", "SZ300308", ["b"])
    _add_rel(conn, "company:旧名", "SUPPLIES_TO", "SZ300308", ["a"])
    tmp_registry.merge("company:旧名", "SZ300502")
    rows = conn.execute("SELECT src, dst, support_count, sources FROM relations").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "SZ300502"
    assert sorted(json.loads(rows[0][3])) == ["a", "b"]
    assert rows[0][2] == 2
    conn.close()


def test_merge_relation_selfloop_deleted(tmp_registry, tmp_path):
    """合并后 src==dst 的自环边直接删,不留无意义自指。"""
    conn = _mk_structure(tmp_path)
    _add_rel(conn, "company:碎片", "COMPETES_WITH", "SZ002371", ["c"])
    tmp_registry.merge("company:碎片", "SZ002371")
    assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
    conn.close()


def test_merge_without_structure_db_is_noop(tmp_registry):
    """没有结构层时 merge 照常工作(纯注册表场景不应报错)。"""
    cid = tmp_registry.resolve("宁德时代", type_="stock", stock_code="300750")
    frag = tmp_registry.resolve("宁德", type_="stock")
    tmp_registry.merge(frag, cid)          # structure.db 不存在 → no-op
    assert tmp_registry.resolve("宁德") == cid

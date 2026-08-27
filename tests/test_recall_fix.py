"""召回缺口修复回归(2026-08-25,docs/RECALL_FIX_PLAN_20260825.md)。

固化三层根因的修复:
R0 低覆盖 company: 实体不再劫持快路径(球硅/燃气轮机案例主因);
R1 FTS5+BM25 替代 LIKE:高频词灌不满名额、稀有词排前、raw-SQL 漂移可对账、缺索引可降级;
P1-D 语义矩阵磁盘缓存跨进程命中。
"""
import json
import sqlite3

import numpy as np
import pytest

from trading_kb.ask import AskEngine
from trading_kb.models import Fact


def _f(subj, claim, cid, level="C", **kw):
    return Fact(subject=subj, predicate="P", object=kw.pop("obj", claim[:20]), canonical_id=cid,
                claim=claim, evidence_level=level, sources=kw.pop("sources", ["s"]), **kw)


def _legacy_company(reg, name: str) -> str:
    """模拟闸门下沉(2026-08-27)之前登记进注册表的存量伪公司行:register() 现在会把主题短语降为
    concept,所以直接 raw INSERT 一行 type=company(生产库里 08-25 前的存量正是这种)。"""
    from trading_kb.models import _normalize
    cid = f"company:{_normalize(name)}"
    reg.conn.execute("INSERT OR IGNORE INTO entities(canonical_id,display_name,type,source) VALUES(?,?,?,?)",
                     (cid, name, "company", "legacy"))
    reg.conn.execute("INSERT OR IGNORE INTO aliases(alias_norm,canonical_id) VALUES(?,?)",
                     (_normalize(name), cid))
    reg.conn.commit()
    return cid


# ── R0:路由按覆盖度 ────────────────────────────────────────────────────────
def test_low_coverage_company_switches_to_discovery(tmp_registry, tmp_facts, tmp_structure):
    """伪公司实体(1 条自有事实)命中查询 → 不走快路径:不做别名过滤、告警出声,球硅事实进结论。"""
    junk = _legacy_company(tmp_registry, "HBM先进封装")                  # 存量伪公司行(无 stock_code)
    tmp_facts.upsert(_f("HBM先进封装", "HBM先进封装 DRIVES 半导体封测:HBM向2.5D/3D封装演进", junk))
    for i, cl in enumerate(["HBM先进封装是高端化学法球硅核心应用场景,拉动球硅需求翻倍",
                            "CoWoS封装光罩尺寸扩大,需更多球硅用于底部填充",
                            "预计2027年高端球硅需求1.64万吨"]):
        tmp_facts.upsert(_f("球硅", cl, "concept:球硅", sources=[f"q{i}"]))
    small = tmp_registry.register("数库科技", type_="company")          # 真公司但只有 1 条事实
    tmp_facts.upsert(_f("数库科技", "数库科技发布一款数据产品", small))
    eng = AskEngine(tmp_registry, tmp_facts, tmp_structure)
    assert eng._locate_entity("球硅 HBM 先进封装 需求") == junk         # 仍会锚到它(注册表未清)
    assert eng._fast_path(junk)[0] is False                             # 短语型:直接拒绝快路径
    assert eng._fast_path(small) == (False, 1)                          # 低覆盖:按事实数拒绝
    res = eng.ask("球硅 HBM 先进封装 需求", use_semantic=False)
    claims = [f["claim"] for f in res.facts]
    assert sum("球硅" in c for c in claims) == 3, claims               # 旧逻辑:别名过滤全丢,只剩 1 条
    assert any("切发现模式" in w for w in res.warnings)


def test_high_coverage_or_listed_keeps_fast_path(tmp_registry, tmp_facts, tmp_structure):
    """真公司(自有事实>=阈值)/ 带 stock_code / 真上市码 → 快路径不变(精智达/长鑫类不回归)。"""
    from trading_kb import config
    rich = tmp_registry.register("长鑫存储", type_="company")
    for i in range(config.FAST_PATH_MIN_FACTS):
        tmp_facts.upsert(_f("长鑫存储", f"长鑫存储事实{i}", rich, sources=[f"r{i}"]))
    listed = tmp_registry.register("精智达", type_="company", stock_code="688627")
    tmp_facts.upsert(_f("精智达", "精智达一条事实", listed))
    eng = AskEngine(tmp_registry, tmp_facts, tmp_structure)
    assert eng._fast_path(rich)[0] is True
    assert eng._fast_path(listed)[0] is True                            # 寡事实但带 stock_code
    assert eng._fast_path("SH688627")[0] is True
    assert eng._fast_path("concept:半导体")[0] is False                   # 非证券恒发现模式
    res = eng.ask("长鑫存储", use_semantic=False)
    assert not any("切发现模式" in w for w in res.warnings)


# ── R1:FTS5 + BM25 ─────────────────────────────────────────────────────────
def test_fts_rare_term_not_buried_by_frequent(tmp_facts):
    """"需求"这类高频 gram 命中几百条也灌不满名额:稀有词"球硅"事实必须排在最前。"""
    assert tmp_facts.fts_status()["built"]                             # 空库建起即启用
    for i in range(300):
        tmp_facts.upsert(_f("N", f"下游需求回暖,行业需求增长第{i}期", "c:n", level="A",
                            sources=[f"n{i}"]))
    for i in range(3):
        tmp_facts.upsert(_f("球硅", f"球硅需求翻倍,第{i}条", "c:q", sources=[f"q{i}"]))
    hits = tmp_facts.search("球硅 需求", limit=50)
    assert [("球硅" in h["claim"]) for h in hits[:3]] == [True, True, True], \
        [h["claim"] for h in hits[:5]]
    assert len(hits) == 50                                              # 名额仍被填满(不是只返稀有词)


def test_fts_reconcile_after_raw_sql(tmp_facts):
    """绕过 FactsStore 的 raw INSERT/DELETE 造成漂移 → fts_build 补缺+清孤儿,search 随之正确。"""
    fid_del = tmp_facts.upsert(_f("A", "鳑鲏鱼将被原生删除", "c:a"))
    con = tmp_facts.conn
    con.execute("DELETE FROM facts WHERE fact_id=?", (fid_del,))         # 模拟治理脚本
    con.execute("""INSERT INTO facts(fact_id,dedup_key,subject,predicate,object,canonical_id,claim,
                   status,evidence_level,unverifiable,source_kind,support_count,sources,valid_at)
                   VALUES('raw1','raw1','B','P','o','c:b','原生插入的鲏鱼事实','active','C',1,'x',1,'[]',NULL)""")
    con.commit()
    assert not any(h["fact_id"] == "raw1" for h in tmp_facts.search("鲏鱼", limit=10))
    r = tmp_facts.fts_build()
    assert r["added"] == 1 and r["removed"] == 1
    st = tmp_facts.fts_status()
    assert st["indexed"] == st["active"]
    assert any(h["fact_id"] == "raw1" for h in tmp_facts.search("鲏鱼", limit=10))
    assert all(h["fact_id"] != fid_del for h in tmp_facts.search("鳑鲏鱼", limit=10))


def test_fts_fallback_to_like_when_not_built(tmp_facts, capsys):
    """索引未启用(存量库首建前)→ 走 LIKE 降级且 stderr 出声;结果仍可召回。"""
    tmp_facts.upsert(_f("A", "稀有关键词鳑鲏鱼在此", "c:a"))
    tmp_facts.conn.execute("DELETE FROM fts_meta WHERE key='built'")
    tmp_facts.conn.commit()
    from trading_kb import facts_store as fs
    fs._WARNED.clear()
    hits = tmp_facts.search("鳑鲏鱼", limit=10)
    assert any("鳑鲏鱼" in h["claim"] for h in hits)
    assert "LIKE 降级" in capsys.readouterr().err
    tmp_facts.fts_build()
    assert tmp_facts.fts_status()["built"]


def test_fts_whole_word_and_stop_gram(tmp_facts):
    """整词"股份"可查(用户明确输入);"股份回购"靠非停用 gram 命中;含 % 的查询不炸。"""
    tmp_facts.upsert(_f("某公司", "某公司发布股份回购方案", "c:a", level="A"))
    tmp_facts.upsert(_f("A", "毛利率50%以上的高成长标的", "c:b", level="B"))
    assert tmp_facts.search("股份回购")
    assert tmp_facts.search("股份")
    assert any("50%以上" in h["claim"] for h in tmp_facts.search("毛利率50%以上", limit=50))
    assert tmp_facts.search('"引号"奇怪 输入 %_') == [] or True            # 不抛异常即可


# ── P1-D:语义矩阵磁盘缓存 ─────────────────────────────────────────────────
def test_semantic_matrix_disk_cache_roundtrip(tmp_path):
    """首次从 BLOB 读 → 落 .npy/.ids.json;第二个实例(模拟新进程)memmap 命中且数值一致;
    向量库变化后指纹失效自动重读。"""
    from trading_kb import semantic as S
    backend = S._BACKENDS[1]                                            # model2vec, vectors.db, 256 维
    facts_db = tmp_path / "facts.db"
    vec_db = tmp_path / backend.vec_db_name
    con = sqlite3.connect(vec_db)
    con.execute("CREATE TABLE vectors (fact_id TEXT PRIMARY KEY, vec BLOB)")
    rng = np.random.default_rng(0)
    for i in range(5):
        con.execute("INSERT INTO vectors VALUES (?,?)",
                    (f"f{i}", rng.random(backend.dim, dtype="float32").tobytes()))
    con.commit(); con.close()
    a = S.SemanticIndex(facts_db, backend)
    a._load_matrix()
    mat_p, ids_p = a._cache_paths()
    assert mat_p.exists() and ids_p.exists()
    assert json.loads(ids_p.read_text())["ids"] == [f"f{i}" for i in range(5)]
    b = S.SemanticIndex(facts_db, backend)
    assert b._load_matrix_cache() is True
    assert isinstance(b._mat, np.memmap) and b._mat.shape == (5, backend.dim)
    assert np.allclose(np.asarray(b._mat), np.asarray(a._mat))
    # 库变了 → 指纹失效
    c = sqlite3.connect(vec_db)
    c.execute("INSERT INTO vectors VALUES ('f5', ?)", (rng.random(backend.dim, dtype="float32").tobytes(),))
    c.commit(); c.close()
    d = S.SemanticIndex(facts_db, backend)
    assert d._load_matrix_cache() is False
    d._load_matrix()
    assert d._mat.shape[0] == 6
    for x in (a, b, d):
        x.close()


# ── P2-F:伪公司闸门 ────────────────────────────────────────────────────────
def test_is_pseudo_company_precision():
    from trading_kb.entity_quality import is_pseudo_company
    for n in ["AI需求", "北美数据中心", "HBM先进封装", "数据中心自备电源", "云厂商", "北美云厂商",
              "美国关税政策", "海外算力", "主流电池企业", "AI资本开支", "全球矿山资本开支",
              "美国电网缺电", "中东局势", "电脑软硬件涨价"]:
        assert is_pseudo_company(n), n
    for n in ["长鑫存储", "华虹半导体", "小鹏汽车", "地平线机器人", "南都电源", "万业企业",
              "粤海投资", "美国银行", "中国宏桥", "三星/SK海力士", "宁波众一驱动技术有限公司",
              "MPLX有限合伙企业", "NVIDIA", "台积电", "周大福企业", "Dell Technologies Inc"]:
        assert not is_pseudo_company(n), n
    assert not is_pseudo_company(None) and not is_pseudo_company("")


def test_ingest_gate_registers_pseudo_company_as_concept(tmp_registry, tmp_facts, tmp_structure):
    from trading_kb.ingest import ResearchIngestor
    eng = ResearchIngestor(tmp_registry, tmp_facts, tmp_structure)
    card = {"id": "c1", "type": "company", "title": "t", "broker": "b",
            "entities": [{"name": "HBM先进封装", "kind": "company"},
                         {"name": "长鑫存储", "kind": "company"}],
            "findings": []}
    from trading_kb.ingest import IngestReport
    eng.ingest_card(card, IngestReport())
    assert tmp_registry.resolve("HBM先进封装") == "concept:hbm先进封装"
    assert tmp_registry.resolve("长鑫存储") == "company:长鑫存储"


def test_fast_path_rejects_rich_pseudo_company(tmp_registry, tmp_facts, tmp_structure):
    """"云厂商"这类短语即使自有事实 ≥ 阈值也不走快路径(与闸门同规则)。"""
    from trading_kb import config
    cid = _legacy_company(tmp_registry, "云厂商")                    # 模拟存量伪实体(raw 行)
    for i in range(config.FAST_PATH_MIN_FACTS + 2):
        tmp_facts.upsert(_f("云厂商", f"云厂商资本开支第{i}期上修", cid, sources=[f"y{i}"]))
    eng = AskEngine(tmp_registry, tmp_facts, tmp_structure)
    assert eng._fast_path(cid) == (False, 0)

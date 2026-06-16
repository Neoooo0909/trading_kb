"""时序事实层测试:去重合并、成色升级、supersede、contradict、include_invalidated。"""
from trading_kb.facts_store import FactsStore
from trading_kb.models import Fact


def _fact(obj="特斯拉减速器定点", sources=None, level="C", **kw):
    return Fact(subject="绿的谐波", predicate="HAS_ORDER_INTENT", object=obj,
                canonical_id="SH688017", claim=obj, evidence_level=level,
                sources=sources or ["doc1"], valid_at="2026-05-29", **kw)


def test_upsert_and_get(tmp_facts):
    fid = tmp_facts.upsert(_fact())
    row = tmp_facts.get(fid)
    assert row["canonical_id"] == "SH688017"
    assert row["status"] == "active"


def test_dedup_merge_accumulates_sources(tmp_facts):
    """同一事实多篇 → 合并,累加来源 + support_count,不新建重复(F11)。"""
    fid1 = tmp_facts.upsert(_fact(sources=["docA"]))
    fid2 = tmp_facts.upsert(_fact(sources=["docB"]))
    assert fid1 == fid2                      # 同一 fact_id
    row = tmp_facts.get(fid1)
    assert row["support_count"] == 2
    import json
    assert set(json.loads(row["sources"])) == {"docA", "docB"}


def test_dedup_upgrades_evidence_level(tmp_facts):
    """合并时按最高信源升级成色。"""
    tmp_facts.upsert(_fact(sources=["docA"], level="C"))
    fid = tmp_facts.upsert(_fact(sources=["docB"], level="A"))
    assert tmp_facts.get(fid)["evidence_level"] == "A"


def test_deterministic_id_idempotent(tmp_facts):
    """重复执行同一事实不重复追加(deterministic id)。"""
    tmp_facts.upsert(_fact(sources=["docA"]))
    tmp_facts.upsert(_fact(sources=["docA"]))
    assert tmp_facts.stats()["total"] == 1


def test_supersede(tmp_facts):
    old = tmp_facts.upsert(_fact(obj="定点意向"))
    new = Fact(subject="绿的谐波", predicate="HAS_CONFIRMED_ORDER", object="正式订单",
               canonical_id="SH688017", claim="已签正式订单", evidence_level="A",
               sources=["docC"], valid_at="2026-07-01")
    tmp_facts.supersede(old, new, at="2026-07-01")
    assert tmp_facts.get(old)["status"] == "superseded"
    assert tmp_facts.get(old)["invalid_at"] == "2026-07-01"
    # 默认检索不返 superseded
    active = tmp_facts.query(canonical_id="SH688017")
    assert all(f["status"] in ("active", "disputed") for f in active)
    # include_invalidated 能查回历史
    allf = tmp_facts.query(canonical_id="SH688017", include_invalidated=True)
    assert any(f["status"] == "superseded" for f in allf)


def test_contradict_not_deleted(tmp_facts):
    """证伪不删除(回滚能力):标 invalidated,可 include_invalidated 查回。"""
    fid = tmp_facts.upsert(_fact())
    tmp_facts.contradict(fid, at="2026-08-01")
    assert tmp_facts.get(fid)["status"] == "invalidated"
    assert tmp_facts.query(canonical_id="SH688017") == []   # 默认不返
    assert len(tmp_facts.query(canonical_id="SH688017", include_invalidated=True)) == 1


def test_query_filters_by_predicate(tmp_facts):
    tmp_facts.upsert(_fact())
    assert tmp_facts.query(predicate="HAS_ORDER_INTENT")
    assert tmp_facts.query(predicate="NO_SUCH") == []

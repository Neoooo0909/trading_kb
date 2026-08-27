"""dedup_same_claim.py 纯函数核心 + 事务内合并(2026-08-26 对抗审查后)。

覆盖:多证券主体组不合并(P1-1)/ 类别硬度优先于 rule_version(P1-2)/ 合并字段并集与归档(P1-3)/
事务内重读:组内行被改动则跳过(P0-1/P0-2)/ 幂等。
"""
import importlib.util
import json
from pathlib import Path

import pytest

from trading_kb.facts_store import FactsStore
from trading_kb.models import Fact

_SPEC = importlib.util.spec_from_file_location(
    "dedup_same_claim", Path(__file__).resolve().parent.parent / "scripts" / "dedup_same_claim.py")
dedup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dedup)


def _put(facts, cid, pred, claim, cat="hard_fact", level="C", doc="d1", extra=None, unver=True):
    f = Fact(subject=cid.split(":")[-1], predicate=pred, object=claim[:80], canonical_id=cid,
             claim=claim, evidence_level=level, unverifiable=unver, sources=[doc], category=cat,
             extra=extra or {})
    fid = facts.upsert(f)
    # 2026-08-27 起 upsert 有 (doc, claim) 判重,同文同句历史重复只能来自旧库;测试里清掉登记模拟存量。
    facts.conn.execute("DELETE FROM doc_claim"); facts.conn.commit()
    return fid


@pytest.fixture
def facts(tmp_path):
    f = FactsStore(tmp_path / "f.db")
    yield f
    f.close()


def test_多证券主体组整组不合并(facts):
    c = "太保一季度个险新单同比下降15.2%，新华保险同比增长122.8%"
    _put(facts, "SH601601", "HAS_FINANCIAL_METRIC", c)
    _put(facts, "SH601336", "HAS_FINANCIAL_METRIC", c)
    groups = dedup.find_groups(facts.conn)
    plan, st = dedup.plan_groups(groups)
    assert len(groups) == 1 and st["skipped_multi_stock"] == 1 and plan == []


def test_类别硬度优先于rule_version_旧hard不被v3view吃掉(facts):
    c = "恒立液压董事长被有关部门拘留"
    old = _put(facts, "SH601100", "HAS_CATALYST", c, cat="hard_fact", level="C")
    new = _put(facts, "SH601100", "HAS_VIEW", c, cat="view", level="D",
               extra={"rule_version": "v3", "entities": ["恒立液压"]})
    plan, st = dedup.plan_groups(dedup.find_groups(facts.conn))
    assert len(plan) == 1
    keeper, losers = plan[0]
    assert keeper["fact_id"] == old and losers[0]["fact_id"] == new
    assert st["groups_category_conflict"] == 1


def test_合并字段并集与归档_事务内执行(facts):
    c = "KOSPI再创历史收盘新高，外资转为净卖出科技板块"
    k = _put(facts, "concept:kospi", "HAS_CATALYST", c, level="C", doc="d1",
             extra={"entities": ["KOSPI"], "doubts": [{"kind": "x", "severity": "low", "message": "a"}],
                    "verified_numbers": 1, "rule_version": "v3"})
    l = _put(facts, "concept:kospi", "HAS_RATING", c, level="B", doc="d1",
             extra={"entities": ["外资"], "doubts": [{"kind": "y", "severity": "low", "message": "b"}],
                    "verified_numbers": 3})
    # 同文:l 再挂一个来源
    facts.conn.execute("UPDATE facts SET sources=?, support_count=2 WHERE fact_id=?",
                       (json.dumps(["d1", "d2"]), l))
    facts.conn.commit()
    plan, _ = dedup.plan_groups(dedup.find_groups(facts.conn))
    assert len(plan) == 1 and plan[0][0]["fact_id"] == k
    dedup._ensure_archive(facts.conn)
    assert dedup.apply_group(facts, plan[0][0], plan[0][1], "2026-08-26 17:00:00") == "merged"
    row = facts.conn.execute("SELECT * FROM facts WHERE fact_id=?", (k,)).fetchone()
    assert json.loads(row["sources"]) == ["d1", "d2"] and row["support_count"] == 2
    assert row["evidence_level"] == "B"                       # 取高
    ex = json.loads(row["extra"])
    assert ex["entities"] == ["KOSPI", "外资"] and ex["verified_numbers"] == 3
    assert {d["message"] for d in ex["doubts"]} == {"a", "b"} and ex["merged_from"] == [l]
    assert facts.conn.execute("SELECT COUNT(*) FROM facts WHERE fact_id=?", (l,)).fetchone()[0] == 0
    arch = facts.conn.execute("SELECT role, fact_id FROM facts_merged_archive ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in arch] == [("loser", l), ("keeper_before", k)]
    # FTS:loser 索引删了,keeper 能按并集实体召回
    assert all(h["fact_id"] != l for h in facts.search("KOSPI", limit=20))
    assert any(h["fact_id"] == k for h in facts.search("外资", limit=20))
    # 幂等:再找一遍无组
    assert dedup.plan_groups(dedup.find_groups(facts.conn))[0] == []


def test_事务内重读_组内行被改动即跳过(facts):
    c = "同一句话"
    k = _put(facts, "concept:a", "HAS_CATALYST", c, doc="d1")
    l = _put(facts, "concept:a", "HAS_FORECAST", c, doc="d1")
    plan, _ = dedup.plan_groups(dedup.find_groups(facts.conn))
    dedup._ensure_archive(facts.conn)
    # 模拟并发:loser 被别的进程标 superseded
    facts.conn.execute("UPDATE facts SET status='superseded' WHERE fact_id=?", (l,))
    facts.conn.commit()
    assert dedup.apply_group(facts, plan[0][0], plan[0][1], "t") == "skipped_changed"
    assert facts.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2
    # 模拟并发:keeper 的 sources 被别的进程追加 → 乐观条件不中 → 跳过且不删 loser
    facts.conn.execute("UPDATE facts SET status='active' WHERE fact_id=?", (l,))
    plan, _ = dedup.plan_groups(dedup.find_groups(facts.conn))
    orig_apply = facts.conn.execute
    # 用 keeper0 的陈旧 sources 构造冲突:先在计划外改 keeper
    facts.conn.execute("UPDATE facts SET sources=? WHERE fact_id=?", (json.dumps(["d1", "d9"]), k))
    facts.conn.commit()
    # plan 里的 keeper0 快照 sources 仍是 ["d1"];事务内重读后 keeper 行 sources=["d1","d9"],
    # UPDATE 带 AND sources=重读值 → 仍能成功(重读即最新),这是正确行为
    assert dedup.apply_group(facts, plan[0][0], plan[0][1], "t") == "merged"
    assert facts.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


def test_rule_rank解析():
    assert dedup._rule_rank({"rule_version": "v3"}) == 3
    assert dedup._rule_rank({}) == 0
    assert dedup._rule_rank({"rule_version": "v12"}) == 12
    assert dedup._rule_rank({"rule_version": "weird"}) > 100

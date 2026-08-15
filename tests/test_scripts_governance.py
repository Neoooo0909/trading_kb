"""治理/运维脚本的纯函数核心测试(2026-08-16 审查补钉)。

审查结论:每晚自动 --apply 写生产库、删备份的脚本几乎零测试,而它们恰是
下一次事故最可能发生的地方。本文件钉住三块最高危的不变量:
  ① clean_entities._reattribute 手抄的主键口径必须与 models.Fact 恒等;
  ② prune_backups 三个删除规划器的保留承诺(月度锚点/成对/IPO 留 2);
  ③ announcements_to_kb.is_substantive 白名单+程序性过滤。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from trading_kb.facts_store import FactsStore          # noqa: E402
from trading_kb.models import Fact                     # noqa: E402


# ── ① _reattribute 主键不变量 ────────────────────────────────────────────
def test_reattribute_key_matches_models_fact(tmp_path):
    """脚本手抄的 dedup_key/sha1 口径若与 models.Fact 漂移,改挂后全库主键静默错配。

    不变量:_reattribute(new_cid) 之后,行内 (fact_id, dedup_key) 必须等于
    用相同 predicate/object/new_cid 构造的 models.Fact 的属性值。
    """
    from clean_entities import _reattribute

    store = FactsStore(tmp_path / "facts.db")
    orig = Fact(subject="旧主体", predicate="HAS_CONFIRMED_ORDER",
                object="中标某某项目5.2亿元", canonical_id="concept:旧主体",
                claim="旧主体 中标", evidence_level="B")
    store.upsert(orig)

    fc = store.conn
    _reattribute(fc, orig.fact_id, "SH600000", "浦发银行")
    fc.commit()

    expect = Fact(subject="浦发银行", predicate="HAS_CONFIRMED_ORDER",
                  object="中标某某项目5.2亿元", canonical_id="SH600000")
    row = fc.execute("SELECT fact_id, dedup_key, canonical_id FROM facts").fetchone()
    assert row["fact_id"] == expect.fact_id, "脚本 sha1 口径与 models.Fact 漂移!"
    assert row["dedup_key"] == expect.dedup_key
    assert row["canonical_id"] == "SH600000"
    store.close()


def test_reattribute_merges_when_target_exists(tmp_path):
    """目标 fact_id 已存在 → 删旧行(合并),不产生重复主键。"""
    from clean_entities import _reattribute

    store = FactsStore(tmp_path / "facts.db")
    a = Fact(subject="A", predicate="HAS_CAPACITY", object="扩产100万吨",
             canonical_id="concept:A", claim="x")
    b = Fact(subject="B", predicate="HAS_CAPACITY", object="扩产100万吨",
             canonical_id="SH600001", claim="y")
    store.upsert(a)
    store.upsert(b)
    _reattribute(store.conn, a.fact_id, "SH600001", "B")
    store.conn.commit()
    rows = store.conn.execute("SELECT COUNT(*) c FROM facts").fetchone()
    assert rows["c"] == 1
    store.close()


# ── ② prune_backups 规划器 ──────────────────────────────────────────────
@pytest.fixture()
def prune(tmp_path, monkeypatch):
    import prune_backups as pb
    monkeypatch.setattr(pb, "_BACKUP_DIR", tmp_path)
    return pb


def _mk(d: Path, name: str):
    (d / name).write_bytes(b"x")


def test_plan_daily_keeps_monthly_anchor(prune, tmp_path):
    """月度锚点永久保留——正是曾被 ZSXQ 一刀切轮转架空、在生产中失守的承诺。"""
    now = datetime.now()
    old_month = (now - timedelta(days=70)).strftime("%Y%m")   # 两个多月前
    # 老月份两对备份(首份=锚点应保留,次份可删)
    for ts in (f"{old_month}01_010000", f"{old_month}15_010000"):
        _mk(tmp_path, f"facts.db.bak.{ts}")
        _mk(tmp_path, f"entities.db.bak.{ts}")
    # 近两天各一对(应保留)
    for i in (0, 1):
        ts = (now - timedelta(days=i)).strftime("%Y%m%d") + "_120000"
        _mk(tmp_path, f"facts.db.bak.{ts}")
        _mk(tmp_path, f"entities.db.bak.{ts}")

    doomed, kept = prune._plan_daily()
    doomed_names = {p.name for p in doomed}
    # 月度锚点(老月份首份)绝不能进删除清单
    assert f"facts.db.bak.{old_month}01_010000" not in doomed_names
    assert f"entities.db.bak.{old_month}01_010000" not in doomed_names
    # 老月份的第二份(非锚点、超保留窗)应被删
    assert f"facts.db.bak.{old_month}15_010000" in doomed_names
    # 近两天的都不能删
    for i in (0, 1):
        ts = (now - timedelta(days=i)).strftime("%Y%m%d") + "_120000"
        assert f"facts.db.bak.{ts}" not in doomed_names


def test_plan_ipo_keeps_latest_two(prune, tmp_path):
    for d in ("2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01"):
        _mk(tmp_path, f"facts.db.bak.ipo_ingest.{d}")
    doomed, kept = prune._plan_ipo()
    names = {p.name for p in doomed}
    assert names == {"facts.db.bak.ipo_ingest.2026-05-01",
                     "facts.db.bak.ipo_ingest.2026-06-01"}


# ── ③ announcements is_substantive ──────────────────────────────────────
def test_is_substantive_whitelist_and_procedural():
    from announcements_to_kb import is_substantive

    # 白名单类别 + 实质标题 → 收
    assert is_substantive("关于中标重大项目的公告", "重大合同/中标") is True
    assert is_substantive("2026年半年度业绩预告", "业绩预告/快报") is True
    # 白名单类别但程序性附件 → 剔
    assert is_substantive("律师事务所关于重大资产重组的法律意见书", "重组并购") is False
    assert is_substantive("独立董事关于股权激励的独立意见", "股权激励/员工持股") is False
    # 非白名单类别 → 剔
    assert is_substantive("第八届董事会第三次会议决议公告", "人事/治理") is False


# ── 双实现收敛的回归锚 ──────────────────────────────────────────────────
def test_to_payload_no_truncation():
    """web JSON 出口不得截断证据链/来源(v0.4 的 [:8]/[:12] 旧 bug 回归锚)。"""
    from trading_kb.ask import AskResult

    facts = []
    for i in range(20):
        facts.append({"fact_id": f"f{i}", "claim": f"论断{i}", "status": "active",
                      "evidence_level": "B", "unverifiable": 0, "support_count": 1,
                      "sources": f'["card_{i:02d}"]', "extra": "{}"})
    res = AskResult(query="q", facts=facts)
    p = res.to_payload()
    assert len(p["evidence"]) == 20          # 旧 bug: active[:8]
    assert len(p["sources"]) == 20           # 旧 bug: srcs[:12]


def test_parse_fragments_shared_impl():
    from trading_kb.sentiment_lane import parse_fragments

    text = ("[2026-06-10 09:30] 绿的谐波要起飞\n"
            "2026-06-11\t宁德时代利空\n"
            "没有时间戳的一条\n"
            "\n")
    frags = parse_fragments(text)
    assert frags == [("绿的谐波要起飞", "2026-06-10 09:30"),
                     ("宁德时代利空", "2026-06-11"),
                     ("没有时间戳的一条", "")]

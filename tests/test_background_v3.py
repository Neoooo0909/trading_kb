"""分流 v3(2026-08-26):view 档 / 数值路径去年份门槛 / background 留痕 / 结论跳 view / 多实体进 FTS。

背景:docs/BACKGROUND_FIX_PLAN_20260826.md——被判 background 丢弃的 finding 里真背景仅 10%。
"""
import json

import pytest

from trading_kb.ask import AskEngine, _top_conclusion
from trading_kb.classify import classify_finding, _has_real_numbers
from trading_kb.entity_registry import EntityRegistry
from trading_kb.facts_store import FactsStore
from trading_kb.ingest import IngestReport, ResearchIngestor
from trading_kb.models import Finding, level_down
from trading_kb.structure_store import StructureStore


def _f(claim, evidence="", numbers=None, entities=None, source_kind="social_research",
       doc_id="zp_test", date="2026-08-09"):
    return Finding(claim=claim, evidence=evidence, numbers=numbers or [],
                   entities=entities or [], source_kind=source_kind,
                   doc_id=doc_id, source_date=date)


# ── classify ─────────────────────────────────────────────────────────────
def test_有主体定性论断判view():
    f = _f("晓程科技拥有海外金矿资产，利润弹性突出。", entities=["晓程科技"])
    assert classify_finding(f) == "view"


def test_无主体无数字仍是background():
    assert classify_finding(_f("我们看好该行业长期发展前景")) == "background"
    assert classify_finding(_f("本周矿业股和整体市场表现强劲。")) == "background"


def test_硬数字无年份判hard_fact():
    # 旧规则要求正文自带年份;卡片日期已是时间锚
    assert classify_finding(_f("外资连续3日净买入SK海力士，今日净买入5.88亿美元",
                               entities=["SK海力士"])) == "hard_fact"
    assert classify_finding(_f("英伟达AI芯片出口至中国受限，每日限额20%",
                               entities=["英伟达"])) == "hard_fact"


def test_numbers字段真量值加主体判hard_fact_纯日期值不算():
    assert classify_finding(_f("下一代AI服务器每机架被动元件需求约33万颗", entities=["英伟达"],
                               numbers=[{"value": "330,000", "context": "颗"}])) == "hard_fact"
    # numbers 只有 "9月" 这种纯日期值 → 不算可证伪数值 → 仍是 view
    f = _f("FCC事件担忧预计9月访美前逐步缓解", entities=["FCC"],
           numbers=[{"value": "9月", "context": "时间"}])
    assert not _has_real_numbers(f)
    assert classify_finding(f) == "view"


def test_垃圾实体不构成主体():
    # 只有垃圾实体(空串/纯标点)的定性句不进 view
    assert classify_finding(_f("市场情绪回暖", entities=["", "  "])) == "background"


def test_v3单调放宽_原判定不变():
    # 原 hard/quant/structure 用例在 v3 下结论不变
    assert classify_finding(_f("公司2026年5月中标特斯拉减速器订单",
                               numbers=[{"value": "1"}])) == "hard_fact"
    assert classify_finding(_f("谐波减速器是人形机器人的上游核心部件",
                               entities=["谐波减速器", "人形机器人"])) == "structure"
    assert classify_finding(_f("多空博弈因子全市场选股效果出色", "RankIC -9.73%",
                               numbers=[{"value": "-9.73%"}])) == "quant_fact"


def test_level_down():
    assert level_down("B") == "C"
    assert level_down("B+") == "B"
    assert level_down("C") == "D"
    assert level_down("D") == "D"


# ── ingest ───────────────────────────────────────────────────────────────
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
        "id": "zp_655ebaec0fe9", "type": "zsxq_post", "date": "2026-08-09",
        "source_kind": "social_research",
        "entities": [{"name": "浩通科技", "kind": "stock"}, {"name": "晓程科技", "kind": "stock"}],
        "findings": [
            {"claim": "晓程科技拥有海外金矿资产，利润弹性突出。", "entities": ["晓程科技"]},
            {"claim": "浩通科技受益贵金属回收业务盈利释放，晓程科技因海外金矿资产利润弹性突出。",
             "entities": ["浩通科技", "晓程科技"]},
            {"claim": "美国7月非农就业人数减少2.3万人，加息预期降温",
             "entities": ["贵金属", "晓程科技"],
             "numbers": [{"value": "2.3万人", "context": "非农"}]},
            {"claim": "黄金板块情绪回暖"},                       # 无主体 → 留痕
            {"claim": "黄金属于贵金属环节", "entities": ["黄金"]},   # structure 不足两端 → view
        ],
    }


def _ingest_card(ing, card, report):
    for f in card["findings"]:
        ing.ingest_finding(Finding(claim=f["claim"], entities=f.get("entities", []),
                                   numbers=f.get("numbers", []), doc_id=card["id"],
                                   source_kind=card["source_kind"], source_date=card["date"]),
                           report, code_map={}, card_entity_names=["浩通科技", "晓程科技"],
                           card=card)


def test_view入库_成色降一档_background留痕(stack):
    reg, facts, structure, ing = stack
    rep = IngestReport()
    _ingest_card(ing, _card(), rep)
    assert rep.views >= 2 and rep.hard_facts >= 1 and rep.background == 1
    rows = facts.query(limit=50)
    views = [r for r in rows if r["category"] == "view"]
    assert views and all(r["predicate"] == "HAS_VIEW" for r in views)
    # social_research 基线 C → view 降到 D,且恒 unverifiable
    assert all(r["evidence_level"] == "D" and r["unverifiable"] for r in views)
    # 硬事实(2.3万人)保持基线 C
    hard = [r for r in rows if r["category"] == "hard_fact"]
    assert hard and hard[0]["evidence_level"] == "C"
    # background 留痕可审计
    assert facts.background_log_count() == 1
    row = facts.conn.execute("SELECT claim, doc_id FROM background_log").fetchone()
    assert row["claim"] == "黄金板块情绪回暖" and row["doc_id"] == "zp_655ebaec0fe9"
    # 幂等:再摄入一次不重复留痕、不重复计事实
    n_before = facts.count_active()
    _ingest_card(ing, _card(), IngestReport())
    assert facts.background_log_count() == 1 and facts.count_active() == n_before


def test_多实体写入extra并进FTS(stack):
    reg, facts, structure, ing = stack
    rep = IngestReport()
    _ingest_card(ing, _card(), rep)
    multi = [r for r in facts.query(limit=50) if "浩通科技受益" in r["claim"]][0]
    extra = json.loads(multi["extra"])
    assert extra["entities"] == ["浩通科技", "晓程科技"] and extra["rule_version"] == "v3"
    # 主体挂浩通科技,但按次要实体名也能被 FTS 召回
    assert multi["subject"] == "浩通科技"
    hits = facts.search("晓程科技", limit=50)
    assert any(h["fact_id"] == multi["fact_id"] for h in hits)
    # set_extra_entities:存量补录改写 FTS
    solo = [r for r in facts.query(limit=50) if r["claim"].startswith("晓程科技拥有")][0]
    assert facts.set_extra_entities(solo["fact_id"], ["晓程科技", "金矿"])
    assert json.loads(facts.conn.execute("SELECT extra FROM facts WHERE fact_id=?",
                                         (solo["fact_id"],)).fetchone()[0])["entities"] == ["晓程科技", "金矿"]
    assert any(h["fact_id"] == solo["fact_id"] for h in facts.search("金矿", limit=50))
    assert not facts.set_extra_entities("no_such_id", ["x"])


# ── ask ──────────────────────────────────────────────────────────────────
def test_结论头条跳过view():
    active = [{"claim": "v", "category": "view"}, {"claim": "h", "category": "hard_fact"}]
    assert _top_conclusion(active)["claim"] == "h"
    assert _top_conclusion([{"claim": "v", "category": "view"}])["claim"] == "v"


def test_ask端到端_view进证据链与情绪面_不做结论(stack):
    reg, facts, structure, ing = stack
    rep = IngestReport()
    _ingest_card(ing, _card(), rep)
    res = AskEngine(reg, facts, structure).ask("晓程科技")
    md = res.to_six_section()
    claims = [f["claim"] for f in res.facts]
    assert any("利润弹性突出" in c for c in claims)          # view 可检索
    assert "情绪面" in md and "利润弹性突出" in md               # D 级进情绪面段
    d = res.to_payload()
    assert d["conclusion"]["claim"].startswith("美国7月非农")   # 结论是硬事实而非 view

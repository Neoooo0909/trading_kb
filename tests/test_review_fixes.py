"""2026-08-06 对抗性审查修复的回归测试。

覆盖:① quant 词边界(裸 "ic" 误判 88% 的修复);② RATING 词典收紧与股东增减持分流;
③ 裸年份边界;④ 黑名单收窄;⑤ relation_for 英文映射;⑥ is_ib_firm 同形上市公司豁免;
⑦ 候选池反饿死(P0-1:重覆盖个股的 C/D 级进得了池、情绪面段渲染);
⑧ 公告澄清证伪 → 订单族 disputed。
"""
import sys
from pathlib import Path

import pytest

from trading_kb.classify import classify_finding, predicate_for, relation_for
from trading_kb.entity_quality import is_ib_firm
from trading_kb.entity_registry import EntityRegistry
from trading_kb.facts_store import FactsStore
from trading_kb.models import Fact, Finding
from trading_kb.structure_store import StructureStore
from trading_kb.ask import AskEngine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def _f(claim, evidence="", numbers=None):
    return Finding(claim=claim, evidence=evidence, numbers=numbers or [])


# ── ① quant 词边界 ────────────────────────────────────────────────────────
def test_quant_英文price不再误判():
    """回归:price/historic 含子串 ic,曾把英文硬事实抢进 quant_fact(实测88%误判)。"""
    assert classify_finding(_f("NdPr oxide price increased 1.3% WoW to RMB729,000/t")) != "quant_fact"
    assert classify_finding(_f("NIO ADR target price USD6.80, upside +12.8%")) != "quant_fact"


def test_quant_真量化仍识别():
    assert classify_finding(_f("该多空因子回测IC为0.05,ICIR 0.8")) == "quant_fact"
    assert classify_finding(_f("The factor backtest shows IC of 0.05")) == "quant_fact"


def test_quant_弱词须双共现():
    assert classify_finding(_f("组合策略年化收益18%,超额6%")) == "quant_fact"   # 弱词>=2
    # 单个弱词(资产配置语境)不算量化
    assert classify_finding(_f("高盛建议做多基本面组合,对铜铝多头平仓")) != "quant_fact"


# ── ② RATING 收紧 + 股东增减持 ────────────────────────────────────────────
def test_rating_真评级动作():
    assert predicate_for(_f("Bernstein维持买入评级,目标价72元")) == "HAS_RATING"
    assert predicate_for(_f("下调至中性,价格目标从$36升至$47")) == "HAS_RATING"
    assert classify_finding(_f("Cadence price target raised to $320")) == "hard_fact"


def test_rating_同形词不再误标():
    # 股东减持 → HAS_INSIDER_TRADE(非评级)
    f = _f("控股股东拟减持不超过2%公司股份")
    assert classify_finding(f) == "hard_fact"
    assert predicate_for(f) == "HAS_INSIDER_TRADE"
    # 宏观预测修正 → 预测,不是评级
    f2 = _f("2026年印尼CPI通胀预测从2.6%上调至2.8%")
    assert predicate_for(f2) == "HAS_FORECAST"
    # "AA+评级"字面含"评级",关键词法收紧后仍会命中——已知残留,文档化防误当回归
    f3 = _f("美国公众持有债务超过GDP的100%,高于其他AA+评级主权国家")
    assert predicate_for(f3) == "HAS_RATING"


def test_rating_基金仓位underweight仍会命中的已知残留():
    """underweight 的仓位语义无法用规则区分,收紧后保留该残留(文档化,防误当回归)。"""
    assert predicate_for(_f("FMS bond allocation collapsed to net 24% underweight")) == "HAS_RATING"


# ── ③ 裸年份边界 ─────────────────────────────────────────────────────────
def test_year_金额与代码不当年份():
    # 纯金额句:2000万 的"2000"不再当年份(_YEAR_RE 不命中)。
    # v3(2026-08-26)起数值兜底不再要求年份——有金额即可证伪、时间锚由卡片日期承担 → hard_fact;
    # 本断言改为只锁"2000 不被当成年份"这一点,分类结论随 v3 口径。
    from trading_kb.classify import _YEAR_RE
    assert not _YEAR_RE.search("拟使用自有资金2000万元购买理财产品")
    assert classify_finding(_f("拟使用自有资金2000万元购买理财产品")) == "hard_fact"
    # 真年份仍可锚定(财务词+数值+年份)
    assert classify_finding(_f("Company revenue grew 15% in 2026")) == "hard_fact"


# ── ④ 黑名单收窄 ─────────────────────────────────────────────────────────
def test_boilerplate_评级定义仍拦_目标价句放行():
    assert classify_finding(_f("买入:评级的12个月总回报预期高于市场15%以上")) == "background"
    assert classify_finding(_f("维持买入评级的12个月目标价为25元")) == "hard_fact"


def test_boilerplate_past_performance只拦免责句式():
    assert classify_finding(_f("Past performance is not indicative of future results")) == "background"
    assert classify_finding(_f("The factor past performance shows IC of 0.05")) == "quant_fact"


# ── ⑤ relation_for 英文映射 ──────────────────────────────────────────────
def test_relation_for_english():
    assert relation_for(_f("Alchip is a key competitor of GUC in ASIC")) == "COMPETES_WITH"
    assert relation_for(_f("Unimicron supplies to Nvidia value chain")) == "SUPPLIES"


# ── ⑥ is_ib_firm 同形豁免 ────────────────────────────────────────────────
def test_is_ib_firm_exceptions():
    assert is_ib_firm("中金") and is_ib_firm("华泰") and is_ib_firm("UBS")
    for legit in ("中金黄金", "中金岭南", "阳谷华泰", "瑞华泰", "金海通", "华泰股份"):
        assert not is_ib_firm(legit), legit


# ── ⑦ 候选池反饿死(P0-1)────────────────────────────────────────────────
@pytest.fixture
def stack(tmp_path):
    reg = EntityRegistry(tmp_path / "e.db")
    facts = FactsStore(tmp_path / "f.db")
    structure = StructureStore(tmp_path / "s.db")
    yield reg, facts, structure
    reg.close(); facts.close(); structure.close()


def test_ask_pool_heavy_coverage_keeps_cd(stack):
    """重覆盖个股(高成色>120条):C/D 级仍进候选池,情绪面段渲染。

    回归:实体路按成色降序 LIMIT 120,曾把 279 只个股(宁德/比亚迪/茅台…)的
    C/D 级全部截掉,情绪面段静默失效(库里 14031 条 C/D 不可达)。
    """
    reg, facts, structure = stack
    cid = reg.resolve("饿死测试股", type_="stock", stock_code="000099")
    for i in range(130):                                    # 130 条 A 级填满旧 120 位
        facts.upsert(Fact(subject="饿死测试股", predicate="HAS_CATALYST",
                          object=f"程序性公告第{i}号", canonical_id=cid,
                          claim=f"饿死测试股程序性公告第{i}号", evidence_level="A",
                          source_kind="official_announcement", valid_at="2026-01-01"))
    c_claims = ["饿死测试股液冷订单有望翻倍(社媒前瞻)", "饿死测试股大客户送样进展顺利"]
    for cl in c_claims:
        facts.upsert(Fact(subject="饿死测试股", predicate="HAS_CATALYST", object=cl,
                          canonical_id=cid, claim=cl, evidence_level="C",
                          source_kind="social_research", valid_at="2026-07-01"))
    res = AskEngine(reg, facts, structure).ask("饿死测试股")
    got = {f["claim"] for f in res.facts if f["evidence_level"] == "C"}
    assert got.issuperset(set(c_claims)), f"C级事实没进候选池: {got}"
    out = res.to_six_section()
    assert "## 情绪面·不同观点" in out
    for cl in c_claims:
        assert cl in out


def test_search_stop_gram_不影响整词查询(stack):
    """"股份"作为切出的 gram 被停用,但作为用户完整查询词仍可检索。"""
    reg, facts, structure = stack
    facts.upsert(Fact(subject="某公司", predicate="HAS_BUYBACK", object="股份回购方案",
                      claim="某公司发布股份回购方案", evidence_level="A"))
    assert facts.search("股份回购")            # 完整词 token 命中
    assert facts.search("股份")                # 纯停用词作为 word token 仍可查


# ── ⑧ 澄清证伪 → disputed ────────────────────────────────────────────────
def test_clarification_disputes_order_rumor(stack):
    reg, facts, structure = stack
    # scripts/ 不入库,fresh clone 无此模块时跳过本条
    ann = pytest.importorskip("announcements_to_kb")
    fid = facts.upsert(Fact(subject="某公司", predicate="HAS_ORDER_RUMOR",
                            object="传闻中标某海外大额订单", canonical_id="SZ000001",
                            claim="市场传某公司中标海外大额订单", evidence_level="C"))
    n = ann._dispute_order_facts(facts, "SZ000001", "关于媒体报道中标海外订单传闻的澄清公告")
    assert n == 1
    assert facts.get(fid)["status"] == "disputed"
    # 不相干标题不误伤
    fid2 = facts.upsert(Fact(subject="某公司", predicate="HAS_ORDER_RUMOR",
                             object="传闻获新能源电池订单", canonical_id="SZ000002",
                             claim="传某公司获新能源电池订单", evidence_level="C"))
    assert ann._dispute_order_facts(facts, "SZ000002", "关于房产处置进展的公告") == 0
    assert facts.get(fid2)["status"] == "active"

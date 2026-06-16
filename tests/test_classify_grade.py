"""分流器 + 双轨成色测试。"""
from trading_kb.classify import classify_finding, predicate_for, relation_for
from trading_kb.grade import grade_fact, baseline_grade
from trading_kb.models import Finding


def _f(claim, evidence="", numbers=None, entities=None, source_kind="broker_research"):
    return Finding(claim=claim, evidence=evidence, numbers=numbers or [],
                   entities=entities or [], source_kind=source_kind)


def test_classify_hard_fact_order():
    f = _f("公司2026年5月中标特斯拉减速器订单", numbers=[{"value": "1", "page": 1}])
    assert classify_finding(f) == "hard_fact"
    assert predicate_for(f) == "HAS_CONFIRMED_ORDER"


def test_classify_quant_fact():
    f = _f("多空博弈因子全市场选股效果出色", "RankIC -9.73%, 年化40.12%",
           numbers=[{"value": "-9.73%", "page": 1}])
    assert classify_finding(f) == "quant_fact"


def test_classify_structure():
    f = _f("谐波减速器是人形机器人的上游核心部件", entities=["谐波减速器", "人形机器人"])
    assert classify_finding(f) == "structure"
    assert relation_for(f) == "UPSTREAM_OF"


def test_classify_background():
    f = _f("我们看好该行业长期发展前景")
    assert classify_finding(f) == "background"


def test_predicate_strength_order():
    # 同时含传闻与确认词,取最强(确认)
    f = _f("传闻公司中标大单", numbers=[{"value": "1", "page": 1}])
    assert predicate_for(f) == "HAS_CONFIRMED_ORDER"


def test_grade_unverifiable_not_refuted():
    """不可验证类:保留信源基线 + unverifiable,绝不降为证伪(铁律5)。"""
    f = _f("产业链景气度向上", source_kind="broker_research")
    level, unver = grade_fact(f, "HAS_CATALYST", verify=None)
    assert level == "B"        # 券商研报基线
    assert unver is True


def test_grade_verifiable_no_hook_keeps_baseline():
    """可验证类但无数据钩子:保留基线 + unverifiable(不假装已验证)。"""
    f = _f("公司中标订单", source_kind="broker_research")
    level, unver = grade_fact(f, "HAS_CONFIRMED_ORDER", verify=None)
    assert level == "B"
    assert unver is True


def test_grade_verifiable_confirmed_upgrades():
    f = _f("公司中标订单", source_kind="broker_research")
    level, unver = grade_fact(f, "HAS_CONFIRMED_ORDER",
                              verify=lambda ff, p: "confirmed")
    assert level == "A"
    assert unver is False


def test_grade_verifiable_not_found_downgrades_not_refutes():
    """查无 ≠ 证伪:温和降级,仍保留 unverifiable。"""
    f = _f("公司中标订单", source_kind="official_announcement")
    level, unver = grade_fact(f, "HAS_CONFIRMED_ORDER", verify=lambda ff, p: None)
    assert level == "B"        # A 降一档到 B
    assert unver is True


def test_baseline_mapping():
    assert baseline_grade("official_announcement") == "A"
    assert baseline_grade("broker_research") == "B"
    assert baseline_grade("social_chat") == "D"
    assert baseline_grade("unknown_kind") == "C"

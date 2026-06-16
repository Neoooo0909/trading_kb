"""深度质疑闭环测试:说法 vs 公告口径核对。"""
from trading_kb.deep_verify import (
    deep_verify_fact, cross_check, code_from_canonical, DeepVerdict)


def _fact(claim, predicate="HAS_CONFIRMED_ORDER", cid="SH688017", category="hard_fact"):
    return {"claim": claim, "predicate": predicate, "canonical_id": cid,
            "category": category, "fact_id": "x"}


def test_code_from_canonical():
    assert code_from_canonical("SH688017") == "688017"
    assert code_from_canonical("SZ300750") == "300750"
    assert code_from_canonical("concept:固态电池") is None
    assert code_from_canonical("stock_pending:绿的谐波") is None


# ── 核对裁决 ──────────────────────────────────────────────────────────────
def test_corroborated():
    """公告(中标类)与说法高度相关 → 佐证。"""
    docs = [{"title": "关于中标特斯拉减速器订单的公告", "category": "重大合同/中标",
             "text": "公司近日中标特斯拉人形机器人减速器订单,金额约5亿元"}]
    v = cross_check("绿的谐波中标特斯拉减速器订单5亿", "HAS_CONFIRMED_ORDER", docs)
    assert v.status == "corroborated"
    assert "重大合同/中标" in v.matched_category


def test_contradicted_by_clarification():
    """澄清类公告且高度相关 → 说法可能被打脸。"""
    docs = [{"title": "关于媒体报道特斯拉订单的澄清公告", "category": "澄清/媒体回应",
             "text": "公司澄清:与特斯拉减速器订单传闻不属实,目前无相关订单"}]
    v = cross_check("绿的谐波中标特斯拉减速器订单", "HAS_CONFIRMED_ORDER", docs)
    assert v.status == "contradicted"


def test_not_disclosed_when_no_relevant():
    """有公告但与说法无关 → 公告无披露,存疑维持。"""
    docs = [{"title": "关于召开股东大会的通知", "category": "股东大会", "text": "定于X日召开"}]
    v = cross_check("绿的谐波中标特斯拉减速器订单", "HAS_CONFIRMED_ORDER", docs)
    assert v.status == "not_disclosed"


def test_not_disclosed_when_empty():
    v = cross_check("某订单", "HAS_CONFIRMED_ORDER", [])
    assert v.status == "not_disclosed"


# ── 适用性 ────────────────────────────────────────────────────────────────
def test_not_applicable_non_hardfact():
    v = deep_verify_fact(_fact("因子表现好", category="quant_fact"))
    assert v.status == "not_applicable"


def test_not_applicable_no_code():
    v = deep_verify_fact(_fact("某产业链定点", cid="concept:机器人"))
    assert v.status == "not_applicable"


# ── 端到端(注入 fetch_fn,不联网)────────────────────────────────────────
def test_deep_verify_with_injected_fetch():
    def fake_fetch(code, category):
        assert code == "688017"
        return [{"title": "中标特斯拉减速器订单公告", "category": "重大合同/中标",
                 "text": "中标特斯拉减速器订单"}]
    v = deep_verify_fact(_fact("绿的谐波中标特斯拉减速器订单"), fetch_fn=fake_fetch)
    assert v.status == "corroborated"

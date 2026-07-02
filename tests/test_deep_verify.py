"""深度质疑闭环测试:说法 vs 公告口径核对。"""
import datetime as _dt

from trading_kb.deep_verify import (
    deep_verify_fact, cross_check, code_from_canonical, DeepVerdict,
    auto_verify_fresh)


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


# ── allow_soft + auto_verify_fresh(成色≠时效价值:新鲜低成色线索自动核验)──────────
def _soft_fact(claim, cid="SZ000725", category="quant_fact", level="C",
               valid_at=None, predicate="HAS_PARTNERSHIP", subject="京东方A", fid="x"):
    return {"claim": claim, "predicate": predicate, "canonical_id": cid,
            "category": category, "evidence_level": level, "valid_at": valid_at,
            "subject": subject, "fact_id": fid}


def _days_ago(n):
    return (_dt.date.today() - _dt.timedelta(days=n)).isoformat()


def test_allow_soft_lifts_hardfact_gate():
    """allow_soft=True 让 quant_fact 也能核(康宁 MOU 这类新边际多挂 quant_fact)。"""
    f = _soft_fact("京东方A与康宁签署三年MOU探索玻璃基封装载板与光互连合作")
    assert deep_verify_fact(f).status == "not_applicable"        # 默认仍按 hard_fact 闸跳过

    def fetch(code, category):
        return [{"title": "京东方与康宁签署合作备忘录推进玻璃基封装载板光互连",
                 "category": "对外投资",
                 "text": "双方就玻璃基封装载板、光互连达成三年合作备忘录"}]
    v = deep_verify_fact(f, fetch_fn=fetch, allow_soft=True)     # 放闸后真正去核
    assert v.status == "corroborated"


def test_auto_verify_fresh_event_dedup_and_best_evidence():
    """选 C/D+近120天+实质;按事件聚类(康宁多措辞合一)取最佳佐证;跳过太老/高成色;3元组返回。"""
    facts = [
        _soft_fact("京东方前瞻布局国内最大单体Micro LED芯片产线行业领先",
                   valid_at=_days_ago(8), fid="micro"),                            # 独立事件
        _soft_fact("康宁 SUPPLIES_TO A股「京东方A」（中性）：京东方A与康宁签署三年MOU推进玻璃基封装与光互连合作",
                   category="structure", valid_at=_days_ago(10), fid="corn_a"),    # 康宁(结构表述,最新)
        _soft_fact("京东方A与康宁签署三年MOU探索玻璃基封装与光互连合作",
                   valid_at=_days_ago(12), fid="corn_b"),                          # 康宁同事件(另一措辞)→合并
        _soft_fact("某旧口径消息已经过时不应再被当作核验对象",
                   valid_at=_days_ago(300), fid="old"),                            # 太老→跳
        _soft_fact("京东方互动答复钙钛矿处于中试阶段加大研发投入",
                   level="A", category="hard_fact", valid_at=_days_ago(5), fid="a_hard"),  # 非C/D→跳
    ]

    def fetch(code, cat):                                          # 给康宁/玻璃基封装事件一条佐证公告
        return [{"title": "京东方与康宁签署合作备忘录推进玻璃基封装与光互连",
                 "category": "对外投资",
                 "text": "双方就玻璃基封装、光互连达成三年合作备忘录"}]
    out = auto_verify_fresh(facts, _dt.date.today().toordinal(), max_n=5, fetch_fn=fetch)
    by_id = {f["fact_id"]: (v, n) for f, v, n in out}             # 3 元组 (fact, verdict, 合并条数)
    assert "old" not in by_id and "a_hard" not in by_id           # 太老/高成色 跳过
    assert "corn_a" in by_id and "corn_b" not in by_id            # 康宁两措辞合并,代表取最新 corn_a
    assert by_id["corn_a"][1] == 2                                # 合并条数=2
    assert by_id["corn_a"][0].status == "corroborated"            # 取最佳佐证:任一措辞命中公告即佐证
    assert "micro" in by_id and by_id["micro"][1] == 1            # Micro LED 独立事件保留


def test_auto_verify_fresh_skips_non_security():
    """无证券码(concept:)的低成色线索不进核验队列。"""
    facts = [_soft_fact("某题材线索玻璃基封装方向", cid="concept:玻璃基",
                         valid_at=_days_ago(3), fid="c")]
    out = auto_verify_fresh(facts, _dt.date.today().toordinal(), fetch_fn=lambda c, k: [])
    assert out == []

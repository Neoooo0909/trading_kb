"""端到端:合成卡片 → 摄入 → 三层 → 六段式问答。"""
import pytest

from trading_kb.entity_registry import EntityRegistry
from trading_kb.facts_store import FactsStore
from trading_kb.structure_store import StructureStore
from trading_kb.ingest import ResearchIngestor, IngestReport
from trading_kb.ask import AskEngine


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
        "id": "card_test_1", "type": "industry", "broker": "测试证券",
        "date": "2026-05-29",
        "entities": [{"name": "绿的谐波", "kind": "stock", "code": "688017"}],
        "findings": [
            {"claim": "绿的谐波2026年5月获特斯拉减速器定点",
             "evidence": "产业调研", "entities": ["绿的谐波"],
             "numbers": [{"value": "1", "page": 2}], "page": 2, "confidence": "medium"},
            {"claim": "谐波减速器属于人形机器人上游环节",
             "entities": ["谐波减速器", "人形机器人"], "page": 3},
            {"claim": "我们长期看好机器人板块", "page": 1},
        ],
    }


def test_e2e_ingest_classifies_and_stores(stack):
    reg, facts, structure, ing = stack
    rep = IngestReport()
    ing.ingest_card(_card(), rep)
    assert rep.cards == 1
    assert rep.hard_facts >= 1           # 定点 → hard_fact
    assert rep.structures >= 1           # 上游 → structure
    assert rep.background >= 1           # 看好 → background
    # 事实落库,挂正确主键
    fs = facts.query(canonical_id="SH688017")
    assert any("定点" in f["claim"] for f in fs)


def test_e2e_ask_six_section(stack):
    reg, facts, structure, ing = stack
    rep = IngestReport()
    ing.ingest_card(_card(), rep)
    engine = AskEngine(reg, facts, structure)
    res = engine.ask("绿的谐波 定点")
    assert res.canonical_id == "SH688017"
    out = res.to_six_section()
    assert "## 结论" in out
    assert "## 引用来源" in out
    assert "card_test_1" in out


def test_ask_evidence_insufficient(stack):
    reg, facts, structure, ing = stack
    engine = AskEngine(reg, facts, structure)
    res = engine.ask("不存在的标的XYZ")
    out = res.to_six_section()
    assert "证据不足" in out


# ── C级情绪面·不同观点(用户要求:C级内容多提炼进结论、标注C级、不设上限)──────────────
from trading_kb.ask import AskResult, _low_grade_views


def _fact(claim, level, status="active"):
    """构造 to_six_section 所需的最小事实字典。"""
    return {"status": status, "claim": claim, "evidence_level": level,
            "unverifiable": 1, "support_count": 1, "extra": "{}", "sources": "[]",
            "object": "", "subject": ""}


def test_c_grade_views_section_rendered_and_positioned():
    """C级 active 事实存在时:渲染"情绪面·不同观点(C级)"段,且紧贴结论、在证据链之前。"""
    res = AskResult(query="某标的", facts=[
        _fact("某标的股东减持结果公告", "A"),
        _fact("某标的27年新签订单有望翻倍(社媒前瞻)", "C"),
        _fact("出海送样海外大厂逻辑正式启动", "C"),
    ])
    out = res.to_six_section()
    assert "## 情绪面·不同观点（C级/低成色）" in out
    assert "27年新签订单有望翻倍" in out                       # C级观点被提炼出来
    # 位置:C级段在结论之后、证据链之前(即便材料尾部被截断也落在窗口内)
    assert out.index("## 结论") < out.index("## 情绪面") < out.index("## 证据链")


def test_c_grade_section_absent_when_no_low_grade():
    """全是 A/B 级时不渲染 C级段(避免空段刷屏)。"""
    res = AskResult(query="某标的", facts=[
        _fact("某标的重大合同公告", "A"), _fact("券商研报投资逻辑", "B"),
    ])
    assert "## 情绪面" not in res.to_six_section()


def test_low_grade_views_filter_dedup_and_order():
    """_low_grade_views:仅取C/D、按内容去重、保序、不设上限。"""
    active = [
        _fact("A级公告", "A"), _fact("C级观点甲", "C"),
        _fact("C级观点甲", "C"),                              # 同观点不同份 → 去重
        _fact("D级传闻乙", "D"), _fact("B级研报", "B"),
    ]
    views = _low_grade_views(active)
    assert [v["claim"] for v in views] == ["C级观点甲", "D级传闻乙"]   # 去重+保序,过滤A/B

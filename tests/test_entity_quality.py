"""实体质量校验 + 主语归属(治本)回归测试。"""
from trading_kb.entity_quality import (attribute_subject, card_subject_entities, is_disclaimer,
                                       is_garbage_entity, is_ib_firm, resolve_claim_subject)
from trading_kb.entity_registry import EntityRegistry, UNKNOWN_CID
from trading_kb.ingest import ResearchIngestor, IngestReport, _pick_subject
from trading_kb.models import Finding


# ── 论断点名主体匹配(治未知主体主力,精度优先)──────────────────────────────
def test_resolve_claim_subject_exactly_one():
    """论断点名卡内恰一个公司 → 归它。"""
    ents = [("Agora", "stock"), ("中软国际", "company"), ("奇安信", "company")]
    assert resolve_claim_subject("中软国际预计FY26收入低个位数增长", ents) == "中软国际"
    assert resolve_claim_subject("奇安信预计2027年预算增加", ents) == "奇安信"


def test_resolve_claim_subject_multi_prefers_single_stock():
    """点名多个但恰一个是股票 → 取股票(汉桑科技 vs 合作方 Tonies)。"""
    ents = [("汉桑科技", "stock"), ("Tonies", "company")]
    assert resolve_claim_subject("汉桑科技与Tonies合作的二代产品量产", ents) == "汉桑科技"


def test_resolve_claim_subject_ambiguous_none():
    """点名多个、无唯一股票 → None(不强归属)。"""
    ents = [("Samsung", "company"), ("SK Hynix", "company")]
    assert resolve_claim_subject("Samsung and SK Hynix rally together", ents) is None
    assert resolve_claim_subject("无任何卡内公司的宏观论断", ents) is None


def test_ib_firm_and_disclaimer():
    assert is_ib_firm("Morgan Stanley") and is_ib_firm("高盛") and is_ib_firm("中金公司")
    assert not is_ib_firm("精智达") and not is_ib_firm("英伟达")
    assert is_disclaimer("Morgan Stanley owns 1% or more of a class")
    assert not is_disclaimer("精智达取得3亿订单")


def test_card_subject_entities_excludes_ib_and_broker():
    """候选剔除投行与作者券商,故 'Mizuho upgrades PLTR' 只会命中 PLTR。"""
    ents = [{"name": "Mizuho", "kind": "company"}, {"name": "PLTR", "kind": "stock"},
            {"name": "2025年", "kind": "concept"}]
    cse = card_subject_entities(ents, broker="高盛")
    assert ("PLTR", "stock") in cse
    assert all(n != "Mizuho" for n, _ in cse)              # 投行剔除
    assert resolve_claim_subject("Mizuho upgrades PLTR to Outperform", cse) == "PLTR"


# ── is_garbage_entity ─────────────────────────────────────────────────────
def test_garbage_rules_hit():
    """日期/法条/地域市场/通用词/纯数值(concept/material) 判垃圾。"""
    assert is_garbage_entity("2025年", "material")
    assert is_garbage_entity("4Q25", "concept")
    assert is_garbage_entity("2026年6月", "concept")
    assert is_garbage_entity("Part 52", "concept")
    assert is_garbage_entity("IRA法案", "concept")
    assert is_garbage_entity("美伊停火协议", "company")
    assert is_garbage_entity("巴西市场", "company")
    assert is_garbage_entity("开工", "concept")
    assert is_garbage_entity("590", "material")
    assert is_garbage_entity("3.5亿元", "material")        # 组合金额(单尾缀正则会漏判)
    assert is_garbage_entity("35亿元", "concept")
    assert is_garbage_entity("100万元", "material")
    assert is_garbage_entity("", "concept")
    assert is_garbage_entity(None, "concept")


def test_garbage_rules_spare_legit():
    """真公司/概念/材料/代码背书实体不得误判。"""
    assert not is_garbage_entity("精智达", "company")
    assert not is_garbage_entity("贵州茅台", "company")
    assert not is_garbage_entity("HBM3E", "material")
    assert not is_garbage_entity("动量因子", "concept")
    assert not is_garbage_entity("1688", "company")        # 阿里 B2B 平台,company 不判纯数值
    assert not is_garbage_entity("688627", "stock")        # 代码背书,永不判垃圾
    assert not is_garbage_entity("沪深300", "index")
    assert not is_garbage_entity("中国市场份额第一的存储测试厂商", "company")  # 长名真实体不误伤


# ── 注册闸 ─────────────────────────────────────────────────────────────────
def test_register_gate_routes_garbage_to_unknown(tmp_registry):
    """垃圾名(无代码)登记 → 归 UNKNOWN_CID,不污染注册表。"""
    assert tmp_registry.register("巴西市场", "company") == UNKNOWN_CID
    assert tmp_registry.register("2025年", "material") == UNKNOWN_CID
    # 注册表里不应出现这些垃圾实体
    rows = tmp_registry.conn.execute(
        "SELECT canonical_id FROM entities WHERE canonical_id LIKE '%巴西市场%' OR canonical_id LIKE '%2025年%'"
    ).fetchall()
    assert rows == []


def test_register_gate_keeps_real_and_coded(tmp_registry):
    """真实体正常登记;带股票代码的即便名字像数字也不拦。"""
    cid = tmp_registry.register("精智达", "stock", stock_code="688627")
    assert cid == "SH688627"
    assert tmp_registry.register("动量因子", "concept") == "concept:动量因子"


# ── _pick_subject ──────────────────────────────────────────────────────────
def _card(title, ents, typ="company", broker=""):
    return {"type": typ, "title": title, "broker": broker,
            "entities": [{"name": n, "kind": k} for n, k in ents]}


def test_pick_subject_skips_garbage():
    """首个实体是垃圾时跳到下一个真实体。"""
    f = Finding(claim="x", entities=["2025年", "精智达"])
    assert _pick_subject(f, {}) == "精智达"


def test_pick_subject_claim_whitelist():
    """无实体、非关系论断 → 按论断点名匹配卡内主体(多公司行业卡也能归)。"""
    f = Finding(claim="中软国际预计FY26收入增长", entities=[])
    assert _pick_subject(f, _card("AI", [("Agora", "stock"), ("中软国际", "company")],
                                  typ="industry")) == "中软国际"


def test_pick_subject_title_dominant():
    """无实体、指代论断 → title 锚定主导主体(个股研报)。"""
    f = Finding(claim="公司存储测试设备业务快速放量", entities=[])
    assert _pick_subject(f, _card("精智达点评", [("精智达", "stock")])) == "精智达"


def test_pick_subject_unknown_when_no_signal():
    """无实体、无点名、非个股研报 → 未知主体。"""
    f = Finding(claim="宏观政策利好", entities=[])
    assert _pick_subject(f, _card("宏观", [], typ="macro")) == "未知主体"


def test_attribute_subject_relationship_no_misattribution():
    """🔴关系/指代论断不得挂到对手方(收购对象/客户/供应商),应归研报标的(对抗审查实锤)。"""
    assert attribute_subject("公司与中科国胜战略合作",
                             _card("五洲新春", [("五洲新春", "stock"), ("中科国胜", "company")])) == "五洲新春"
    assert attribute_subject("公司收购湖北东神天神51%股权",
                             _card("凯龙股份", [("凯龙股份", "stock"), ("湖北东神天神", "company")])) == "凯龙股份"
    assert attribute_subject("结构件给特斯拉供货",
                             _card("东山精密", [("东山精密", "stock"), ("特斯拉", "company")])) == "东山精密"
    assert attribute_subject("公司成为京东方唯一供应商",
                             _card("路维光电", [("路维光电", "stock"), ("京东方", "company")])) == "路维光电"
    assert attribute_subject("Morgan Stanley owns 1% or more", _card("x", [("PLTR", "stock")])) is None


# ── ingest 集成:单主体研报回填 + 垃圾不入表 ───────────────────────────────
def _ingestor(tmp_registry, tmp_facts, tmp_structure):
    return ResearchIngestor(tmp_registry, tmp_facts, tmp_structure)


def test_single_subject_card_attributes_empty_finding(tmp_registry, tmp_facts, tmp_structure):
    """单主体研报里无实体的硬事实 → 归属到该股票(治未知主体)。"""
    # 带订单关键词 + 日期 → 规则判 hard_fact;entities 空 → 应回填到 card_primary 精智达
    card = {
        "id": "c1", "type": "company", "date": "2026-06-09", "broker": "广发证券",
        "entities": [{"name": "精智达", "kind": "stock", "code": "688627"}],
        "findings": [{"claim": "2026年6月存储测试设备中标超3亿元重大合同订单",
                      "entities": [], "numbers": [{"value": "3", "context": "订单金额"}]}],
    }
    _ingestor(tmp_registry, tmp_facts, tmp_structure).ingest_card(card, IngestReport())
    rows = tmp_facts.query(canonical_id="SH688627", limit=10)
    assert any("3亿" in r["claim"] for r in rows)          # 无实体论断挂到了精智达


def test_multicompany_card_attributes_by_claim_mention(tmp_registry, tmp_facts, tmp_structure):
    """多公司研报里无实体的硬事实,按论断点名归到被点名公司(非卡片单主体)。"""
    card = {
        "id": "c3", "type": "industry", "date": "2026-06-09", "broker": "高盛",
        "entities": [{"name": "Agora", "kind": "stock"}, {"name": "中软国际", "kind": "company"}],
        "findings": [{"claim": "2026年6月中软国际中标超3亿元AI重大合同订单",
                      "entities": [], "numbers": [{"value": "3", "context": "金额"}]}],
    }
    _ingestor(tmp_registry, tmp_facts, tmp_structure).ingest_card(card, IngestReport())
    cid = tmp_registry.resolve("中软国际")
    assert any("中软国际" in r["claim"] for r in tmp_facts.query(canonical_id=cid, limit=10))


def test_garbage_card_entity_not_registered(tmp_registry, tmp_facts, tmp_structure):
    """卡片里的垃圾实体不进注册表(闸生效)。"""
    card = {
        "id": "c2", "type": "company", "date": "2026-06-01", "broker": "x",
        "entities": [{"name": "巴西市场", "kind": "company"},
                     {"name": "精智达", "kind": "stock", "code": "688627"}],
        "findings": [{"claim": "测试设备放量", "entities": ["精智达"]}],
    }
    _ingestor(tmp_registry, tmp_facts, tmp_structure).ingest_card(card, IngestReport())
    g = tmp_registry.conn.execute(
        "SELECT * FROM entities WHERE canonical_id LIKE '%巴西市场%'").fetchall()
    assert g == []                                          # 垃圾未入表

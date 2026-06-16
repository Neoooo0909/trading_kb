"""实体注册表测试:归一、别名、合并、code 前缀。"""
from trading_kb.entity_registry import EntityRegistry, _to_market_code


def test_market_code_prefix():
    assert _to_market_code("688017") == "SH688017"
    assert _to_market_code("000001") == "SZ000001"
    assert _to_market_code("300750") == "SZ300750"
    assert _to_market_code("830799") == "BJ830799"
    assert _to_market_code("SH600519") == "SH600519"


def test_stock_resolves_by_code(tmp_registry):
    cid = tmp_registry.resolve("绿的谐波", type_="stock", stock_code="688017")
    assert cid == "SH688017"
    # 同名再解析,拿到同一主键
    assert tmp_registry.resolve("绿的谐波", type_="stock") == "SH688017"


def test_concept_canonical(tmp_registry):
    cid = tmp_registry.resolve("固态电池", type_="concept")
    assert cid.startswith("concept:")
    # 归一:大小写/空白无关
    assert tmp_registry.resolve(" 固态电池 ", type_="concept") == cid


def test_alias_and_merge(tmp_registry):
    cid = tmp_registry.resolve("宁德时代", type_="stock", stock_code="300750")
    tmp_registry.add_alias("CATL", cid)
    assert tmp_registry.resolve("CATL") == cid
    # 合并:把一个碎片实体并入主实体
    frag = tmp_registry.resolve("宁德", type_="stock")
    tmp_registry.merge(frag, cid)
    assert tmp_registry.resolve("宁德") == cid


def test_stats(tmp_registry):
    tmp_registry.resolve("A", "concept")
    tmp_registry.resolve("B", "stock")   # pending
    s = tmp_registry.stats()
    assert s["entities"] >= 2
    assert s["pending_stocks"] >= 1

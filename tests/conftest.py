"""测试公共夹具:把 src 加入 path,提供临时数据目录与隔离存储。"""
import sys
from pathlib import Path

import pytest

# 让 import trading_kb 生效
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture
def tmp_registry(tmp_path):
    from trading_kb.entity_registry import EntityRegistry
    r = EntityRegistry(tmp_path / "e.db")
    yield r
    r.close()


@pytest.fixture
def tmp_facts(tmp_path):
    from trading_kb.facts_store import FactsStore
    f = FactsStore(tmp_path / "f.db")
    yield f
    f.close()


@pytest.fixture
def tmp_structure(tmp_path):
    from trading_kb.structure_store import StructureStore
    s = StructureStore(tmp_path / "s.db")
    yield s
    s.close()

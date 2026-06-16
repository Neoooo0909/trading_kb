#!/usr/bin/env python3
"""无 pytest 时的兜底测试运行器:自动发现 tests/test_*.py 的 test_ 函数并跑。

支持 conftest 的 fixture(tmp_registry/tmp_facts/tmp_structure/lane/stack)的极简版,
但优先用 pytest;此脚本仅作环境无 pytest 时的冒烟兜底。
"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    try:
        import pytest  # noqa
        print("检测到 pytest,转用 pytest 运行。")
        return pytest.main(["-q", str(ROOT / "tests")])
    except ImportError:
        pass

    print("未装 pytest,跑冒烟兜底(端到端 + 关键单元)...")
    import tempfile
    failed = 0

    # 端到端冒烟(不依赖 fixture)
    from trading_kb.entity_registry import EntityRegistry, _to_market_code
    from trading_kb.facts_store import FactsStore
    from trading_kb.structure_store import StructureStore
    from trading_kb.ingest import ResearchIngestor, IngestReport
    from trading_kb.ask import AskEngine
    from trading_kb.models import Fact

    checks = []

    def check(name, cond):
        checks.append((name, cond))
        print(f"  {'✓' if cond else '✗'} {name}")

    check("market_code", _to_market_code("688017") == "SH688017")

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        reg = EntityRegistry(dp / "e.db")
        facts = FactsStore(dp / "f.db")
        st = StructureStore(dp / "s.db")
        ing = ResearchIngestor(reg, facts, st)
        rep = IngestReport()
        card = {"id": "c1", "type": "industry", "broker": "T", "date": "2026-05-29",
                "entities": [{"name": "绿的谐波", "kind": "stock", "code": "688017"}],
                "findings": [
                    {"claim": "绿的谐波2026年5月获特斯拉减速器定点",
                     "entities": ["绿的谐波"], "numbers": [{"value": "1", "page": 2}], "page": 2},
                    {"claim": "谐波减速器属于人形机器人上游环节",
                     "entities": ["谐波减速器", "人形机器人"], "page": 3},
                    {"claim": "长期看好机器人板块", "page": 1}]}
        ing.ingest_card(card, rep)
        check("ingest_hard_fact", rep.hard_facts >= 1)
        check("ingest_structure", rep.structures >= 1)
        check("ingest_background", rep.background >= 1)

        # 去重合并
        f = Fact(subject="绿的谐波", predicate="X", object="o", canonical_id="SH688017",
                 sources=["a"])
        facts.upsert(f)
        facts.upsert(Fact(subject="绿的谐波", predicate="X", object="o",
                          canonical_id="SH688017", sources=["b"]))
        row = [r for r in facts.query(canonical_id="SH688017") if r["predicate"] == "X"][0]
        check("dedup_merge", row["support_count"] == 2)

        engine = AskEngine(reg, facts, st)
        res = engine.ask("绿的谐波 定点")
        check("ask_locates_entity", res.canonical_id == "SH688017")
        check("ask_six_section", "## 结论" in res.to_six_section())
        reg.close(); facts.close(); st.close()

    failed = sum(1 for _, c in checks if not c)
    print(f"\n{'全部通过' if failed == 0 else f'{failed} 项失败'} ({len(checks)} 检查)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

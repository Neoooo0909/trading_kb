"""排位快照:对 RECALL_FIX_PLAN §3 的 5 个案例跑 AskEngine(不走 LLM 合成),记录目标事实的 F 序号与前 20 条 claim。
回填前后各跑一次比对,验收"硬事实排位不退"。用法: PYTHONPATH=src python3 docs/background_fix_scratch/rank_snapshot.py > docs/background_fix_scratch/rank_<before|after>.md"""
import re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.home()/"trading_kb/src"))
from trading_kb import config
from trading_kb.ask import AskEngine
from trading_kb.entity_registry import EntityRegistry
from trading_kb.facts_store import FactsStore
from trading_kb.structure_store import StructureStore
CASES = [
    ("球硅 HBM 先进封装 需求", ["球硅"]),
    ("燃气轮机 数据中心 自备电源 受益标的", ["杰瑞", "东方电气", "潍柴"]),
    ("精智达", ["精智达"]), ("银轮股份", ["银轮"]), ("长鑫存储", ["长鑫"]),
]
reg = EntityRegistry(config.ENTITY_DB); facts = FactsStore(config.FACTS_DB); st = StructureStore(config.STRUCTURE_DB)
eng = AskEngine(reg, facts, st)
print(f"# 排位快照 {time.strftime('%Y-%m-%d %H:%M')} | active={facts.count_active()}\n")
for q, kws in CASES:
    t = time.time(); res = eng.ask(q); dt = time.time() - t
    active = [f for f in res.facts if f["status"] == "active"]
    print(f"## {q}  ({dt:.1f}s, 候选 {len(active)} 条, 警告 {len(res.warnings)})")
    for kw in kws:
        pos = [i for i, f in enumerate(active, 1) if kw in (f["claim"] or "")]
        print(f"- `{kw}` 命中 {len(pos)} 条, 前 5 个序号: {pos[:5]}")
    n_view = sum(1 for f in active if f.get("category") == "view")
    print(f"- view 条数 {n_view} | 结论: {res.to_six_section().splitlines()[2][:80] if len(res.to_six_section().splitlines())>2 else ''}")
    for i, f in enumerate(active[:20], 1):
        print(f"  F{i:<3}[{f['evidence_level']}/{f.get('category','')[:4]}] {f['claim'][:60]}")
    print()

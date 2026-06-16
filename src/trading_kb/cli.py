"""命令行入口。

  python -m trading_kb.cli ingest [--limit N]      # 研报重 lane 摄入
  python -m trading_kb.cli ask "查询"  [--audit]   # 六段式问答
  python -m trading_kb.cli stats                    # 三层规模统计
  python -m trading_kb.cli sentiment-demo           # 舆情轻 lane 演示
"""
from __future__ import annotations

import argparse
import sys

from . import config
from .ask import AskEngine
from .entity_registry import EntityRegistry
from .facts_store import FactsStore
from .ingest import run_ingest
from .sentiment_lane import SentimentLane
from .structure_store import StructureStore


def cmd_ingest(args) -> None:
    rep = run_ingest(limit=args.limit)
    print("=== 摄入回执(研报重 lane)===")
    print(f"卡片 {rep.cards} | findings {rep.findings} | 实体登记 {rep.entities_registered}")
    print(f"硬事实 {rep.hard_facts} | 量化事实 {rep.quant_facts} | "
          f"结构关系 {rep.structures} | 背景(跳过) {rep.background}")
    print(f"成色分布 {rep.level_dist}")
    print(f"质疑标记 {rep.doubts} 条(其中高严重度 {rep.doubt_high} 条)→ 用 `./tkb critique` 看清单")


def cmd_ask(args) -> None:
    config.ensure_data_dir()
    reg = EntityRegistry(config.ENTITY_DB)
    facts = FactsStore(config.FACTS_DB)
    structure = StructureStore(config.STRUCTURE_DB)
    engine = AskEngine(reg, facts, structure)
    res = engine.ask(args.query, include_invalidated=args.audit)
    print(res.to_six_section())
    reg.close(); facts.close(); structure.close()


def cmd_stats(args) -> None:
    config.ensure_data_dir()
    reg = EntityRegistry(config.ENTITY_DB)
    facts = FactsStore(config.FACTS_DB)
    structure = StructureStore(config.STRUCTURE_DB)
    print("=== 三层规模 ===")
    print("实体注册表:", reg.stats())
    print("时序事实层:", facts.stats())
    print("结构关系层:", structure.stats())
    reg.close(); facts.close(); structure.close()


def cmd_critique(args) -> None:
    """列出最该质疑的事实(按严重度排序)。"""
    import json
    config.ensure_data_dir()
    facts = FactsStore(config.FACTS_DB)
    rows = facts.query(include_invalidated=False, limit=5000)
    rank = {"high": 3, "medium": 2, "low": 1}
    scored = []
    for f in rows:
        try:
            extra = json.loads(f.get("extra") or "{}")
        except Exception:
            extra = {}
        doubts = extra.get("doubts") or []
        if doubts:
            sev = extra.get("doubt_severity")
            scored.append((rank.get(sev, 0), f, doubts))
    scored.sort(key=lambda x: -x[0])
    n = args.top or 15
    print(f"=== 最该质疑的 {min(n, len(scored))} 条(共 {len(scored)} 条带质疑)===")
    for _, f, doubts in scored[:n]:
        print(f"\n• {f['claim'][:60]}  [{f['evidence_level']}级]")
        for d in doubts:
            icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(d.get("severity"), "•")
            print(f"    {icon} {d.get('message','')}")
    facts.close()


def cmd_deep_check(args) -> None:
    """深度质疑闭环:对可疑硬事实拉公告正文核对口径(需联网)。"""
    import json
    from .deep_verify import deep_verify_fact
    config.ensure_data_dir()
    facts = FactsStore(config.FACTS_DB)
    rows = facts.query(include_invalidated=False, limit=5000)
    # 选:硬事实 + 带证券代码 + 有质疑标记
    cand = []
    for f in rows:
        if f.get("category") != "hard_fact":
            continue
        cid = f.get("canonical_id", "")
        if not (cid[:2] in ("SH", "SZ", "BJ") and cid[2:].isdigit()):
            continue
        try:
            has_doubt = bool(json.loads(f.get("extra") or "{}").get("doubts"))
        except Exception:
            has_doubt = False
        if has_doubt or args.all:
            cand.append(f)
    if not cand:
        print("没有可深度核对的硬事实(需:hard_fact + 证券代码 + 质疑标记)。")
        print("提示:量化研报语料里硬事实少;行业/公司研报语料下此功能价值更大。")
        facts.close(); return

    print(f"=== 深度核对 {min(len(cand), args.top)} 条可疑硬事实(联网拉公告)===")
    for f in cand[:args.top]:
        v = deep_verify_fact(f)
        print(f"\n• {f['claim'][:50]}  [{f['canonical_id']}]")
        print(f"    {v.tag()}")
        if v.matched_title:
            print(f"    对应公告:[{v.matched_category}] {v.matched_title[:40]}")
        # 回写:被公告打脸→disputed;获佐证→可清乐观存疑(此处仅标注)
        if v.status == "contradicted":
            facts.mark_disputed(f["fact_id"])
            print("    → 已标记 disputed(说法与公告相悖)")
    facts.close()


def cmd_sentiment_demo(args) -> None:
    """舆情轻 lane 演示:用合成碎片跑通(本地无真实聊天数据)。

    C4:演示用独立 demo 库,不污染生产 entities.db/sentiment.db。
    """
    import tempfile
    from pathlib import Path
    demo_dir = Path(tempfile.mkdtemp(prefix="tkb_demo_"))
    reg = EntityRegistry(demo_dir / "entities.db")
    lane = SentimentLane(demo_dir / "sentiment.db", reg)
    watch = ["绿的谐波", "宁德时代", "贵州茅台"]
    frags = [
        ("绿的谐波这波要起飞了,机器人定点稳了", "2026-06-10 09:30"),
        ("宁德时代利空,产能过剩要跌", "2026-06-10 10:15"),
        ("今天大盘没方向,随便聊聊", "2026-06-10 11:00"),   # 无关注标的→冷存
        ("绿的谐波又有人在传特斯拉加单", "2026-06-11 14:00"),
    ]
    for text, ts in frags:
        item = lane.ingest_fragment(text, ts, watch)
        print(f"{'[入库]' if item else '[冷存]'} {text[:30]}")
    print("\n聚合(绿的谐波):", lane.aggregate(reg.resolve("绿的谐波", "stock")))
    print("舆情 lane 统计:", lane.stats())
    reg.close(); lane.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="trading_kb")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="研报重 lane 摄入")
    pi.add_argument("--limit", type=int, default=None)
    pi.set_defaults(func=cmd_ingest)

    pa = sub.add_parser("ask", help="六段式问答")
    pa.add_argument("query")
    pa.add_argument("--audit", action="store_true", help="含历史/反证(include_invalidated)")
    pa.set_defaults(func=cmd_ask)

    ps = sub.add_parser("stats", help="三层规模统计")
    ps.set_defaults(func=cmd_stats)

    pc = sub.add_parser("critique", help="列出最该质疑的事实")
    pc.add_argument("--top", type=int, default=15)
    pc.set_defaults(func=cmd_critique)

    pdc = sub.add_parser("deep-check", help="深度核对:可疑硬事实拉公告正文核对口径(联网)")
    pdc.add_argument("--top", type=int, default=10)
    pdc.add_argument("--all", action="store_true", help="不限于有质疑标记的")
    pdc.set_defaults(func=cmd_deep_check)

    pd = sub.add_parser("sentiment-demo", help="舆情轻 lane 演示")
    pd.set_defaults(func=cmd_sentiment_demo)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

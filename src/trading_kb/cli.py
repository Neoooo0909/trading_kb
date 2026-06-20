"""命令行入口。

  python -m trading_kb.cli ingest [--limit N]      # 研报重 lane 摄入
  python -m trading_kb.cli ask "查询"  [--audit]   # 六段式问答
  python -m trading_kb.cli stats                    # 三层规模统计
  python -m trading_kb.cli sentiment-demo           # 舆情轻 lane 演示
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from . import config
from .ask import AskEngine
from .entity_registry import EntityRegistry
from .facts_store import FactsStore
from .ingest import run_ingest
from .sentiment_lane import SentimentLane
from .structure_store import StructureStore

# 行首时间戳:[2026-06-10 09:30] 内容 / 2026-06-10 09:30\t内容 / 2026-06-10 内容
_TS_RE = re.compile(
    r"^\s*\[?(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)\]?\s*[\t,:：]?\s*(.*)$"
)


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
    six = res.to_six_section()
    if config.USE_LLM:                       # C：Sonnet 合成自然语言回答
        from .llm import synthesize_answer
        ans = synthesize_answer(args.query, six)
        if ans:
            print(ans)
            print("\n" + "─" * 60 + "\n## 📎 检索材料(六段骨架)\n")
    print(six)
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


def _read_fragments(path: Path) -> list[tuple[str, str]]:
    """读聊天/短评文件 → [(文本, 时间戳)]。

    规则:每非空行一条;行首可带时间戳(YYYY-MM-DD 选带 HH:MM[:SS]),
    支持 `[..]`、`\\t`、`,`、`:` 等常见分隔;无时间戳则时间戳留空。
    """
    out: list[tuple[str, str]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TS_RE.match(line)
        if m and m.group(2).strip():        # 行首确为时间戳且后面还有正文
            out.append((m.group(2).strip(), m.group(1)))
        else:
            out.append((line, ""))
    return out


def cmd_feed_chat(args) -> None:
    """舆情轻 lane 入口:把聊天记录/短评文件逐条入库(实体过滤 + 冷存留底)。

    关注标的池:--watch 显式优先;否则用注册表里已有的股票实体(先 ./tkb ingest 建池)。
    """
    config.ensure_data_dir()
    path = Path(args.file).expanduser()
    if not path.exists():
        print(f"✗ 找不到文件:{path}")
        return
    reg = EntityRegistry(config.ENTITY_DB)
    lane = SentimentLane(config.SENTIMENT_DB, reg)
    if args.watch:
        watch = [w.strip() for w in re.split(r"[,，]", args.watch) if w.strip()]
    else:
        watch = reg.watch_terms()
    if not watch:
        print("⚠️  关注标的池为空:注册表暂无股票实体,且未传 --watch。")
        print('    先 `./tkb ingest` 入研报建立标的池,或显式 `--watch "绿的谐波,宁德时代"`。')
        reg.close(); lane.close(); return

    stance_fn = None
    if config.USE_LLM:                       # B：碎片立场走 LLM
        from .llm import make_llm_stance
        stance_fn = make_llm_stance()
    frags = _read_fragments(path)
    kept = cold = 0
    for text, ts in frags:
        item = lane.ingest_fragment(text, ts, watch, llm=stance_fn)
        if item:
            kept += 1
        else:
            cold += 1
    print("=== 舆情摄入回执(轻 lane)===")
    print(f"碎片 {len(frags)} | 命中关注标的入库 {kept} | 噪声冷存留底 {cold}")
    print(f"关注标的池 {len(watch)} 个 | 舆情 lane 统计 {lane.stats()}")
    print("提示:碎片默认 D 级、不进研报证据链;被 B+ 信源印证后才升级。")
    reg.close(); lane.close()


def cmd_add(args) -> None:
    """一条龙:PDF → report_lab 抽卡(ingest/extract/verify)→ tkb 提纯入三层库。

    把"研报变结构化卡片"与"卡片入知识库"两段串成一条命令。
    第 ② 步走模型抽取(Kimi→DeepSeek→Sonnet 降级),耗时且可能消耗 API 额度。
    """
    scripts = config.REPORT_LAB / "scripts"
    if not scripts.exists():
        print(f"✗ 找不到 report_lab/scripts({scripts}),无法抽卡。")
        print("  研报抽取依赖 report_lab;若只想入已有卡片,直接 `./tkb ingest`。")
        return
    paths = [str(Path(p).expanduser()) for p in args.paths]
    batch_args = ["--batch", args.batch] if args.batch else []
    steps = [
        (["python3", "ingest.py", *paths, *batch_args], "① PDF 入库 + 判型(0 token)"),
        (["python3", "extract.py"], "② 模型抽卡片(Kimi→DeepSeek→Sonnet 降级,耗时/可能耗 API)"),
        (["python3", "verify.py"], "③ 数字回原文校验(0 token)"),
    ]
    for cmd, desc in steps:
        print(f"\n▶ {desc}\n  $ (cd {scripts}) {' '.join(cmd)}", flush=True)
        r = subprocess.run(cmd, cwd=str(scripts))
        if r.returncode != 0:
            print(f"✗ 该步失败(returncode={r.returncode}),已中止。")
            return
    print("\n▶ ④ tkb 提纯入三层库")
    cmd_ingest(argparse.Namespace(limit=None))


def cmd_web(args) -> None:
    """启动本地 Web 控制台(单页,标准库,仅绑 127.0.0.1)。"""
    from .web import serve
    serve(port=args.port, open_browser=not args.no_open)


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

    pi = sub.add_parser("ingest", help="研报重 lane 摄入(读 report_lab 已有卡片)")
    pi.add_argument("--limit", type=int, default=None)
    pi.set_defaults(func=cmd_ingest)

    padd = sub.add_parser("add", help="一条龙:PDF→report_lab抽卡→tkb入库")
    padd.add_argument("paths", nargs="+", help="PDF 文件或目录")
    padd.add_argument("--batch", default=None, help="批次名(report_lab raw/ 下子目录)")
    padd.set_defaults(func=cmd_add)

    pfc = sub.add_parser("feed-chat", help="舆情轻 lane:聊天记录/短评文件入库")
    pfc.add_argument("file", help="文本文件(每行一条,行首可带时间戳)")
    pfc.add_argument("--watch", default=None,
                     help='关注标的(逗号分隔);省略则用注册表已有股票池')
    pfc.set_defaults(func=cmd_feed_chat)

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

    pw = sub.add_parser("web", help="启动本地 Web 控制台(单页可视化)")
    pw.add_argument("--port", type=int, default=8765)
    pw.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    pw.set_defaults(func=cmd_web)

    pd = sub.add_parser("sentiment-demo", help="舆情轻 lane 演示")
    pd.set_defaults(func=cmd_sentiment_demo)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

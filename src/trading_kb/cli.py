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
    _peers = [p.strip() for p in (getattr(args, "peers", "") or "").split(",") if p.strip()]
    res = engine.ask(args.query, include_invalidated=args.audit,
                     use_semantic=(True if getattr(args, "semantic", False) else None),
                     revalue=(True if getattr(args, "revalue", False) else None),
                     peers=_peers or None)
    six = res.to_six_section()

    # 时效线索自动交叉核验(仅 USE_WEB 时联网,尊重默认离线可复现):挑新鲜+低成色+实质的
    # 线索拉公告比对可靠性,结论既贴进骨架、也喂给合成层——让"康宁 MOU"这类新边际拿到独立
    # 核实结论(corroborated/not_disclosed/contradicted),而非空标一个 C 级。被公告打脸→disputed。
    verify_block = ""
    if config.USE_WEB:
        from datetime import date as _date
        from .deep_verify import auto_verify_fresh
        pairs = auto_verify_fresh(res.facts, _date.today().toordinal())   # 默认 max_n=5,按事件去重
        if pairs:
            vl = ["## 🔎 时效线索·交叉核验(联网公告)"]
            for f, v, n in pairs:
                merged = f"（合并 {n} 条同事件，取最佳佐证）" if n > 1 else ""
                vl.append(f"- {f['claim'][:48]}  [{f.get('evidence_level')}级/{f.get('valid_at')}]{merged}")
                vl.append(f"    {v.tag()}")
                if v.matched_title:
                    vl.append(f"    对应公告:[{v.matched_category}] {v.matched_title[:40]}")
                if v.status == "contradicted":
                    facts.mark_disputed(f["fact_id"])
                    vl.append("    → 已标记 disputed(与公告相悖)")
            verify_block = "\n".join(vl)

    if config.USE_LLM:                       # C：Sonnet 合成自然语言回答
        from .llm import synthesize_answer
        # 核验结论前置:确保不被 material 截断,且作为最新已核实信号被合成层重点纳入
        material = (verify_block + "\n\n" + six) if verify_block else six
        ans = synthesize_answer(args.query, material)
        if ans:
            print(ans)
            print("\n" + "─" * 60 + "\n## 📎 检索材料(六段骨架)\n")
    print(six)
    if verify_block:
        print("\n" + verify_block)
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
    if not path.is_file():            # is_file 而非 exists:目录/空串("."→目录)不应进 read_text
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
    if not path.is_file():            # is_file:空串归一为目录"."、传目录都会绕过 exists() 后崩 read_text
        print(f"✗ 找不到文件(需为普通文件):{path}")
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
    # 入口预检:全部路径都不存在就别启动流水线——②extract 是耗 API 额度的抽卡步,
    # 敲错路径不应空跑白耗额度(report_lab 对坏文件只标 FAIL 但整批 returncode=0,挡不住)。
    missing = [p for p in paths if not Path(p).exists()]
    if missing and len(missing) == len(paths):
        print("✗ 以下路径均不存在,已中止(不消耗 API):")
        for p in missing:
            print(f"   - {p}")
        return
    if missing:
        print(f"⚠️  {len(missing)} 个路径不存在,将跳过:{', '.join(missing)}")
        paths = [p for p in paths if Path(p).exists()]
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


def cmd_hyp(args) -> None:
    """假设追踪(P1):一假设一 H*.md，证据按 for/against+成色累积、自动估置信度。"""
    from .hypothesis import HypothesisStore
    config.ensure_data_dir()
    hs = HypothesisStore(config.DATA_DIR)
    if args.action in ("show", "evidence", "resolve") and not hs.exists(args.text):
        print(f"✗ 假设 {args.text or '(空)'} 不存在。用 `./tkb hyp list` 看现有假设。")
        return
    if args.action == "new":
        if not args.text:
            print("用法: ./tkb hyp new \"假设标题\" [--ticker 688627] [--statement 详述]"); return
        hid = hs.new(args.text, ticker=args.ticker, statement=args.statement)
        print(f"✓ 新建假设 {hid}: {args.text}  → {hs._path(hid)}")
    elif args.action == "list":
        rows = hs.list_all()
        if not rows:
            print("(暂无假设，用 ./tkb hyp new 创建)"); return
        print(f"=== 活假设 {len(rows)} 条 ===")
        for r in rows:
            print(f"  {r['id']} [{r.get('status','?')}] 置信{r.get('confidence','?')} "
                  f"证据{r.get('n_evidence',0)}条 | {r.get('ticker','')} {r.get('title','')}")
    elif args.action == "show":
        print(hs.get(args.text))
    elif args.action == "evidence":
        if not args.text or not args.ev:
            print("用法: ./tkb hyp evidence H001 --ev \"证据\" --side for|against --grade B"); return
        conf = hs.add_evidence(args.text, args.ev, side=args.side, grade=args.grade)
        print(f"✓ {args.text} +证据[{args.side}/{args.grade}]，置信度→{conf:.2f}")
    elif args.action == "resolve":
        if not args.text or not args.ev:
            print("用法: ./tkb hyp resolve H001 --verdict confirmed --ev \"结论\""); return
        hs.resolve(args.text, args.verdict, args.ev)
        print(f"✓ {args.text} 结案[{args.verdict}]")


def cmd_friction(args) -> None:
    """记一条摩擦日志到 data/friction-log.md(append-only，驱动系统改进)。"""
    from .hypothesis import append_friction
    config.ensure_data_dir()
    append_friction(config.DATA_DIR, args.text)
    print(f"✓ 已记录摩擦 → {config.DATA_DIR / 'friction-log.md'}")


def cmd_debate(args) -> None:
    """真多空对抗辩论(P2):多头→空头逐条反驳→风控裁决。需 TKB_USE_LLM=1。"""
    config.ensure_data_dir()
    if not config.USE_LLM:
        print("⚠ 真对抗需 LLM。请 export TKB_USE_LLM=1 后重试。")
        return
    from .debate import debate, render
    reg = EntityRegistry(config.ENTITY_DB)
    facts = FactsStore(config.FACTS_DB)
    structure = StructureStore(config.STRUCTURE_DB)
    print(render(debate(args.query, AskEngine(reg, facts, structure))))
    reg.close(); facts.close(); structure.close()


def cmd_deep(args) -> None:
    """真 agent loop 深度研究(P2):plan→动态取证(库/行情/财务)→汇总。需 TKB_USE_LLM=1。"""
    config.ensure_data_dir()
    if not config.USE_LLM:
        print("⚠ 深度研究需 LLM。请 export TKB_USE_LLM=1 后重试。")
        return
    from .deep_ask import deep_ask
    reg = EntityRegistry(config.ENTITY_DB)
    facts = FactsStore(config.FACTS_DB)
    structure = StructureStore(config.STRUCTURE_DB)
    r = deep_ask(args.query, AskEngine(reg, facts, structure), tools=_build_data_tools())
    print("## 研究过程")
    for a, arg in r["steps"]:
        print(f"  → {a}: {arg}")
    print("\n## 回答\n" + r["answer"])
    reg.close(); facts.close(); structure.close()


def cmd_semantic(args) -> None:
    """语义索引(P0.5)：build 增量建向量 / status 看覆盖。bge 优先、model2vec 兜底。"""
    from .semantic import SemanticIndex
    config.ensure_data_dir()
    # build/status 显式定后端（默认 bge）；ask 才用 prefer=None 自动择"有数据"的后端
    idx = SemanticIndex.shared(config.FACTS_DB, prefer=(getattr(args, "prefer", None) or "bge"))
    if idx is None:
        print("✗ 语义层不可用(.venv-embed/模型/numpy 缺失)。检查 .venv-embed 与模型目录。")
        return
    facts = FactsStore(config.FACTS_DB)
    n_facts = facts.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE status IN ('active','disputed')").fetchone()[0]
    if args.action == "status":
        total = idx._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        print("=== 语义索引状态 ===")
        print(f"后端: {idx.backend.name} ({idx.backend.dim} 维) | 向量库: {idx.vec_db.name}")
        print(f"已建向量: {total} | 应建事实(active/disputed): {n_facts} | 覆盖 {total}/{n_facts}")
        if total < n_facts:
            print(f"  ⚠ 缺 {n_facts - total} 条未建，跑 `./tkb semantic build` 增量补。")
    else:  # build
        print(f"▶ 增量建索引(后端 {idx.backend.name}/{idx.backend.dim}维，应建 {n_facts} 条)…"
              "大库首次较慢，请耐心。", flush=True)
        n = idx.build(facts)
        total = idx._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        print(f"✓ 新增 {n} 条向量，索引共 {total} 条 → {idx.vec_db}")
    facts.close()


def _build_data_tools() -> dict:
    """尽力接入 tdx(行情)/ifind(财务)作为 deep_ask 工具;接不上则该工具缺省提示未接入。"""
    tools: dict = {}
    try:
        sys.path.insert(0, str(Path.home() / "tdx"))
        from tdx import TdxData
        _t = TdxData()
        tools["quote"] = lambda code: str(_t.quote(code))[:600]
    except Exception:
        pass
    try:
        sys.path.insert(0, str(Path.home()))
        from ifind_ft import iFindFT
        _ft = iFindFT()

        def _fin(code: str) -> str:
            c = code if "." in code else (f"{code}.SH" if code[:1] == "6" else f"{code}.SZ")
            df = _ft.hxds(c, ["ths_revenue_stock"], "2025-01-01", "2026-03-31",
                          interval="Q", days="Alldays")
            return df.to_string()[:600] if hasattr(df, "to_string") else str(df)
        tools["finance"] = _fin
    except Exception:
        pass
    return tools


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
    pa.add_argument("--semantic", action="store_true", help="强制语义召回(较慢，扩召回相关标的)")
    pa.add_argument("--revalue", action="store_true",
                    help="C·环境感知重估:拉实时量价/估值,把事实放进当前定价框架(联网,较慢)")
    pa.add_argument("--peers", default="",
                    help="同业池(逗号分隔证券码,如 002371,688082),用于算相对同业α分离板块beta")
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

    ph = sub.add_parser("hyp", help="假设追踪:new/list/show/evidence/resolve(P1)")
    ph.add_argument("action", choices=["new", "list", "show", "evidence", "resolve"])
    ph.add_argument("text", nargs="?", default="", help="new:标题 / 其余:假设ID")
    ph.add_argument("--ticker", default="", help="标的代码(new)")
    ph.add_argument("--statement", default="", help="假设详述(new)")
    ph.add_argument("--ev", default="", help="证据/结论内容(evidence/resolve)")
    ph.add_argument("--side", choices=["for", "against"], default="for")
    ph.add_argument("--grade", default="C", help="证据成色 A/B+/B/C/D")
    ph.add_argument("--verdict", choices=["confirmed", "refuted", "partial"], default="partial")
    ph.set_defaults(func=cmd_hyp)

    pfr = sub.add_parser("friction", help="记一条摩擦日志(驱动系统改进)")
    pfr.add_argument("text")
    pfr.set_defaults(func=cmd_friction)

    pdb = sub.add_parser("debate", help="真多空对抗辩论(P2，需 TKB_USE_LLM=1)")
    pdb.add_argument("query")
    pdb.set_defaults(func=cmd_debate)

    pdp = sub.add_parser("deep", help="真 agent loop 深度研究(P2，需 TKB_USE_LLM=1)")
    pdp.add_argument("query")
    pdp.set_defaults(func=cmd_deep)

    psm = sub.add_parser("semantic", help="语义索引:build 增量建向量 / status 看覆盖(P0.5)")
    psm.add_argument("action", choices=["build", "status"])
    psm.add_argument("--prefer", choices=["bge", "model2vec"], default=None,
                     help="强制后端(默认自动择优:bge>model2vec)")
    psm.set_defaults(func=cmd_semantic)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

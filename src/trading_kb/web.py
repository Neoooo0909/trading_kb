"""本地单页 Web 控制台(标准库 http.server,零新依赖)。

  ./tkb web   →  http://127.0.0.1:8765

提供四件事,全在一个页面里:
  🔍 问答    六段式回答,成色/质疑彩色渲染
  💬 投喂    粘贴聊天/短评碎片 → 舆情轻 lane
  📊 概览    三层规模 + 一键重摄入(读 report_lab 卡片)
  ⚠ 质疑     最该质疑的结论清单

研报 PDF 入库是重模型任务(抽卡耗 API),仍走 CLI `./tkb add`。
只绑定 127.0.0.1,本机自用,不对外暴露。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .ask import AskEngine
from .entity_registry import EntityRegistry
from .facts_store import FactsStore
from .sentiment_lane import SentimentLane
from .structure_store import StructureStore


# ── 业务:把引擎结果转成前端要的 JSON ─────────────────────────────────────────
def _json_load(s: str, default):
    try:
        return json.loads(s or "")
    except Exception:
        return default


def ask_payload(query: str, audit: bool) -> dict:
    """跑一次问答,产出结构化六段(供前端上色),不复用文本渲染。"""
    config.ensure_data_dir()
    reg = EntityRegistry(config.ENTITY_DB)
    facts = FactsStore(config.FACTS_DB)
    structure = StructureStore(config.STRUCTURE_DB)
    try:
        res = AskEngine(reg, facts, structure).ask(query, include_invalidated=audit)
        active = [f for f in res.facts if f["status"] == "active"]
        out: dict = {
            "query": query,
            "canonical_id": res.canonical_id,
            "found": bool(res.facts or res.neighbors),
            "warnings": list(res.warnings),
            "conclusion": None,
            "evidence": [],
            "doubts": [],
            "conflicts": {"disputed": [], "invalidated": []},
            "followup": [],
            "sources": [],
            "neighbors": [],
            "synthesis": None,
        }
        if config.USE_LLM and (res.facts or res.neighbors):   # C：Sonnet 合成
            from .llm import synthesize_answer
            out["synthesis"] = synthesize_answer(query, res.to_six_section())
        if active:
            top = active[0]
            te = _json_load(top.get("extra"), {})
            out["conclusion"] = {
                "claim": top["claim"], "level": top["evidence_level"],
                "unverifiable": bool(top["unverifiable"]),
                "doubt": te.get("doubt_severity"),
            }
        for i, f in enumerate(active[:8], 1):
            e = _json_load(f.get("extra"), {})
            out["evidence"].append({
                "idx": i, "claim": f["claim"], "level": f["evidence_level"],
                "unverifiable": bool(f["unverifiable"]),
                "support": f["support_count"], "verified": e.get("verified_numbers", 0),
                "doubt": e.get("doubt_severity"),
            })
            for d in (e.get("doubts") or []):
                out["doubts"].append({"idx": i, "severity": d.get("severity"),
                                      "message": d.get("message", "")})
        out["conflicts"]["disputed"] = [f["claim"] for f in res.facts
                                        if f["status"] == "disputed"]
        out["conflicts"]["invalidated"] = [
            {"claim": f["claim"], "status": f["status"]}
            for f in res.invalidated_facts[:5]]
        out["followup"] = [f["claim"][:80] for f in active if f["unverifiable"]][:5]
        srcs = sorted({s for f in active for s in _json_load(f.get("sources"), [])})
        out["sources"] = srcs[:12]
        out["neighbors"] = [{"rel": n.get("rel_type", ""), "name": n.get("dst", "")}
                            for n in res.neighbors[:8]]
        return out
    finally:
        reg.close(); facts.close(); structure.close()


def feed_payload(text: str, watch: str) -> dict:
    """粘贴的聊天/短评文本逐条入舆情轻 lane,回执命中/冷存。"""
    import re
    from .cli import _TS_RE
    config.ensure_data_dir()
    reg = EntityRegistry(config.ENTITY_DB)
    lane = SentimentLane(config.SENTIMENT_DB, reg)
    try:
        terms = ([w.strip() for w in re.split(r"[,，]", watch) if w.strip()]
                 if watch.strip() else reg.watch_terms())
        if not terms:
            return {"ok": False, "msg": "关注标的池为空:先在「概览」里重摄入研报建池,或在下方填写关注标的。"}
        frags = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _TS_RE.match(line)
            if m and m.group(2).strip():
                frags.append((m.group(2).strip(), m.group(1)))
            else:
                frags.append((line, ""))
        stance_fn = None
        if config.USE_LLM:                   # B：碎片立场走 LLM
            from .llm import make_llm_stance
            stance_fn = make_llm_stance()
        kept = cold = 0
        for t, ts in frags:
            if lane.ingest_fragment(t, ts, terms, llm=stance_fn):
                kept += 1
            else:
                cold += 1
        return {"ok": True, "total": len(frags), "kept": kept, "cold": cold,
                "watch": len(terms), "stats": lane.stats()}
    finally:
        reg.close(); lane.close()


def stats_payload() -> dict:
    """三层规模 + 舆情 lane 概览。"""
    config.ensure_data_dir()
    reg = EntityRegistry(config.ENTITY_DB)
    facts = FactsStore(config.FACTS_DB)
    structure = StructureStore(config.STRUCTURE_DB)
    lane = SentimentLane(config.SENTIMENT_DB, reg)
    try:
        return {"entities": reg.stats(), "facts": facts.stats(),
                "structure": structure.stats(), "sentiment": lane.stats()}
    finally:
        reg.close(); facts.close(); structure.close(); lane.close()


def critique_payload(top: int = 20) -> dict:
    """最该质疑的事实(按严重度排序)。"""
    config.ensure_data_dir()
    facts = FactsStore(config.FACTS_DB)
    try:
        rows = facts.query(include_invalidated=False, limit=5000)
        rank = {"high": 3, "medium": 2, "low": 1}
        scored = []
        for f in rows:
            extra = _json_load(f.get("extra"), {})
            doubts = extra.get("doubts") or []
            if doubts:
                sev = extra.get("doubt_severity")
                scored.append((rank.get(sev, 0), f, doubts))
        scored.sort(key=lambda x: -x[0])
        items = []
        for _, f, doubts in scored[:top]:
            items.append({"claim": f["claim"][:80], "level": f["evidence_level"],
                          "flags": [{"severity": d.get("severity"),
                                     "message": d.get("message", "")} for d in doubts]})
        return {"total": len(scored), "items": items}
    finally:
        facts.close()


def ingest_payload() -> dict:
    """一键重摄入 report_lab 卡片(0-token,可反复跑)。"""
    from .ingest import run_ingest
    rep = run_ingest()
    return {"cards": rep.cards, "findings": rep.findings,
            "hard_facts": rep.hard_facts, "quant_facts": rep.quant_facts,
            "structures": rep.structures, "entities": rep.entities_registered,
            "level_dist": rep.level_dist, "doubts": rep.doubts,
            "doubt_high": rep.doubt_high}


# ── HTTP 处理 ────────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    """单页前端 + /api/* JSON 接口。每请求自建 DB 连接,线程安全。"""

    def log_message(self, *a):           # 静默,不刷屏
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return _json_load(self.rfile.read(n).decode("utf-8"), {})

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/stats":
            self._json(stats_payload())
        elif path == "/api/critique":
            self._json(critique_payload())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        try:
            if path == "/api/ask":
                b = self._body()
                self._json(ask_payload(b.get("query", "").strip(),
                                       bool(b.get("audit"))))
            elif path == "/api/feed":
                b = self._body()
                self._json(feed_payload(b.get("text", ""), b.get("watch", "")))
            elif path == "/api/ingest":
                self._json(ingest_payload())
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:                       # 不让单次异常打死服务
            self._json({"error": f"{type(e).__name__}: {e}"}, code=500)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    """启动本地 Web 控制台(仅绑 127.0.0.1)。"""
    addr = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(addr, _Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"📊 trading_kb Web 控制台已启动 → {url}")
    print("   Ctrl+C 退出。")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()


# ── 前端(单文件,内嵌)────────────────────────────────────────────────────────
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>trading_kb · 投研信息处理中枢</title>
<style>
:root{
  --bg:#f5f6fb; --card:#ffffff; --ink:#1a1c2b; --sub:#6b7088; --line:#e8e9f3;
  --accent:#7c3aed; --accent2:#6366f1; --accent-soft:#f0ecff;
  --A:#16a34a; --B:#2563eb; --C:#d97706; --D:#6b7280;
  --hi:#ef4444; --mid:#f59e0b; --lo:#eab308;
  --shadow:0 1px 2px rgba(20,22,50,.04),0 8px 24px rgba(20,22,50,.06);
  --radius:16px;
}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;
  background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased;line-height:1.6}
.wrap{max-width:920px;margin:0 auto;padding:0 20px 80px}
/* header */
header{background:linear-gradient(120deg,#7c3aed 0%,#6366f1 60%,#5b8def 100%);color:#fff;
  padding:30px 0 64px;position:relative;overflow:hidden}
header::after{content:"";position:absolute;inset:0;background:
  radial-gradient(600px 200px at 85% -20%,rgba(255,255,255,.18),transparent 60%)}
.hwrap{max-width:920px;margin:0 auto;padding:0 20px;position:relative;z-index:1}
.brand{display:flex;align-items:center;gap:12px}
.brand h1{font-size:22px;margin:0;font-weight:700;letter-spacing:.2px}
.brand .logo{font-size:26px}
.tag{margin:8px 0 0;opacity:.92;font-size:14px;max-width:640px}
.pills{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}
.pill{background:rgba(255,255,255,.16);backdrop-filter:blur(6px);border:1px solid rgba(255,255,255,.22);
  padding:6px 13px;border-radius:999px;font-size:12.5px;font-weight:600;display:flex;gap:6px;align-items:center}
.pill b{font-weight:700}
/* tabs */
nav{display:flex;gap:6px;margin:-34px auto 0;max-width:920px;padding:0 20px;position:relative;z-index:2}
.tab{flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-bottom:none;
  padding:11px 18px;border-radius:14px 14px 0 0;font-size:14px;font-weight:600;color:var(--sub);
  cursor:pointer;transition:.18s;box-shadow:var(--shadow)}
.tab:hover{color:var(--ink)}
.tab.on{color:var(--accent);background:var(--card)}
.tab.on .dot{background:var(--accent)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:0 var(--radius) var(--radius) var(--radius);
  box-shadow:var(--shadow);padding:24px;min-height:340px}
/* search */
.searchbar{display:flex;gap:10px;align-items:stretch}
input[type=text],textarea{width:100%;font:inherit;color:var(--ink);background:#fbfbfe;
  border:1.5px solid var(--line);border-radius:12px;padding:13px 15px;transition:.15s;outline:none}
input[type=text]:focus,textarea:focus{border-color:var(--accent2);background:#fff;
  box-shadow:0 0 0 4px var(--accent-soft)}
textarea{resize:vertical;min-height:150px;font-size:14px;line-height:1.7}
.btn{background:linear-gradient(120deg,var(--accent),var(--accent2));color:#fff;border:none;
  border-radius:12px;padding:0 22px;font:inherit;font-weight:700;cursor:pointer;white-space:nowrap;
  transition:.18s;box-shadow:0 4px 14px rgba(124,58,237,.28)}
.btn:hover{filter:brightness(1.06);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn.ghost{background:#fff;color:var(--accent);border:1.5px solid var(--line);box-shadow:none}
.btn.sm{padding:9px 16px;font-size:13px}
.opts{display:flex;gap:14px;align-items:center;margin-top:13px;font-size:13px;color:var(--sub)}
.opts label{display:flex;gap:7px;align-items:center;cursor:pointer;user-select:none}
.hint{font-size:12.5px;color:var(--sub);margin-top:10px}
.hint code{background:#f0f0f7;padding:1px 6px;border-radius:5px;font-size:12px}
/* result blocks */
.sec{margin-top:22px;animation:rise .35s ease}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.sec h3{font-size:13px;letter-spacing:.5px;color:var(--sub);text-transform:uppercase;
  margin:0 0 11px;font-weight:700;display:flex;align-items:center;gap:8px}
.sec h3::before{content:"";width:4px;height:14px;border-radius:3px;background:var(--accent)}
.concl{background:linear-gradient(120deg,#faf8ff,#f3f0ff);border:1px solid #e9e3ff;border-radius:14px;
  padding:17px 18px;font-size:16px;font-weight:600;line-height:1.55;display:flex;gap:12px;align-items:flex-start}
.ev{border:1px solid var(--line);border-radius:12px;padding:13px 15px;margin-bottom:9px;
  display:flex;gap:12px;align-items:flex-start;transition:.15s;background:#fff}
.ev:hover{border-color:#d8d9ec;box-shadow:var(--shadow)}
.ev .fid{font:600 12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--sub);
  background:#f4f4fb;border-radius:6px;padding:2px 7px;flex:0 0 auto;margin-top:2px}
.ev .body{flex:1;min-width:0}
.ev .meta{font-size:12px;color:var(--sub);margin-top:6px;display:flex;gap:12px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:700;
  padding:3px 9px;border-radius:999px;flex:0 0 auto}
.badge.A{color:var(--A);background:#e7f6ec}.badge.B{color:var(--B);background:#e7effd}
.badge.C{color:var(--C);background:#fcf1e0}.badge.D{color:var(--D);background:#eef0f2}
.badge .v{opacity:.7;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto;margin-top:7px}
.dot.high{background:var(--hi);box-shadow:0 0 0 3px rgba(239,68,68,.16)}
.dot.medium{background:var(--mid);box-shadow:0 0 0 3px rgba(245,158,11,.16)}
.dot.low{background:var(--lo);box-shadow:0 0 0 3px rgba(234,179,8,.16)}
.doubtbox{background:#fff8f3;border:1px solid #ffe2cf;border-radius:14px;padding:15px 17px}
.doubtbox .lead{font-size:12.5px;color:#b4541a;margin-bottom:10px;font-weight:600}
.drow{display:flex;gap:10px;align-items:flex-start;padding:6px 0;font-size:14px}
.drow .fid{font:600 11px ui-monospace,monospace;color:var(--sub);margin-top:3px}
.chip{display:inline-block;background:#f4f4fb;border:1px solid var(--line);border-radius:8px;
  padding:5px 11px;font-size:13px;margin:0 7px 7px 0}
.muted{color:var(--sub);font-size:14px}
.srcs{display:flex;flex-wrap:wrap;gap:7px}
.src{font:12px ui-monospace,monospace;color:var(--sub);background:#f6f6fc;border:1px solid var(--line);
  padding:4px 9px;border-radius:7px}
.warn{background:#fffbea;border:1px solid #ffe9a8;color:#8a6d1a;border-radius:12px;padding:11px 14px;
  font-size:13px;margin-top:14px}
/* stats grid */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-top:6px}
.stat{border:1px solid var(--line);border-radius:14px;padding:16px 17px;background:linear-gradient(160deg,#fff,#fafaff)}
.stat .k{font-size:12.5px;color:var(--sub);font-weight:600}
.stat .n{font-size:28px;font-weight:800;margin-top:4px;letter-spacing:-.5px}
.stat .x{font-size:12px;color:var(--sub);margin-top:3px}
.lvbar{display:flex;height:10px;border-radius:6px;overflow:hidden;margin-top:10px;border:1px solid var(--line)}
.lvbar i{display:block}
.empty{text-align:center;color:var(--sub);padding:60px 20px}
.empty .ic{font-size:40px;opacity:.5}
.spin{display:inline-block;width:15px;height:15px;border:2px solid rgba(255,255,255,.4);
  border-top-color:#fff;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
.hidden{display:none}
footer{text-align:center;color:var(--sub);font-size:12.5px;margin-top:26px}
footer code{background:#ececf5;padding:2px 7px;border-radius:5px}
</style>
</head>
<body>
<header>
  <div class="hwrap">
    <div class="brand"><span class="logo">📊</span><h1>trading_kb</h1></div>
    <p class="tag">A股个人投研信息处理中枢 —— 研报、聊天、短评一股脑丢进来,自动过滤提纯成分了成色、核了数字、会质疑、可追溯的私人投研大脑。</p>
    <div class="pills" id="pills">
      <span class="pill">⏳ 加载概览…</span>
    </div>
  </div>
</header>

<nav>
  <div class="tab on" data-t="ask">🔍 问答</div>
  <div class="tab" data-t="feed">💬 投喂聊天</div>
  <div class="tab" data-t="stats">📊 概览</div>
  <div class="tab" data-t="crit">⚠ 质疑榜</div>
</nav>

<div class="wrap">
  <!-- 问答 -->
  <section class="panel" id="p-ask">
    <div class="searchbar">
      <input id="q" type="text" placeholder='问点什么，比如：综合量价 因子 / 绿的谐波 定点 / 多空因子 选股效果' autocomplete="off">
      <button class="btn" id="askBtn">查询</button>
    </div>
    <div class="opts">
      <label><input type="checkbox" id="audit"> 含历史/反证（显示已证伪、被替代的旧事实）</label>
    </div>
    <div id="askOut"></div>
  </section>

  <!-- 投喂 -->
  <section class="panel hidden" id="p-feed">
    <p class="muted" style="margin-top:0">把聊天记录 / 群消息 / 短评粘进来，<b>一行一条</b>。提到关注标的的留下入库，其余自动冷存。默认 D 级隔离，不污染研报证据链。</p>
    <textarea id="frag" placeholder="2026-06-10 09:30 绿的谐波这波要起飞，机器人定点稳了&#10;[2026-06-11 14:00] 宁德时代利空，产能过剩要跌&#10;今天大盘没方向，随便聊聊   ← 没提关注标的，自动冷存"></textarea>
    <div class="searchbar" style="margin-top:12px">
      <input id="watch" type="text" placeholder='关注标的（逗号分隔，留空＝用已入库股票池）：绿的谐波,宁德时代'>
      <button class="btn" id="feedBtn">投喂</button>
    </div>
    <div class="hint">时间戳可写可不写，格式随意（<code>[]</code>、Tab、空格都认）。研报 PDF 入库走命令行 <code>./tkb add 文件</code>。</div>
    <div id="feedOut"></div>
  </section>

  <!-- 概览 -->
  <section class="panel hidden" id="p-stats">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <h3 class="sec" style="margin:0">三层知识库规模</h3>
      <button class="btn ghost sm" id="reingestBtn">⟳ 重摄入 report_lab 卡片</button>
    </div>
    <div id="statsOut"><div class="empty">加载中…</div></div>
  </section>

  <!-- 质疑榜 -->
  <section class="panel hidden" id="p-crit">
    <p class="muted" style="margin-top:0">自动批判性体检：没出处的猜测、过于乐观的数字（同类分位对照）、回测软肋。<b>不代表结论一定错</b>，提醒别全信。</p>
    <div id="critOut"><div class="empty">加载中…</div></div>
  </section>

  <footer>本机自用 · 仅绑定 127.0.0.1 · 数据存 <code>data/*.db</code> · 关闭终端即停服</footer>
</div>

<script>
const $=s=>document.querySelector(s), esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const LV={A:'A级',B:'B级',C:'C级',D:'D级'};
function badge(level,unver){return `<span class="badge ${esc(level)}">${LV[level]||level+'级'}${unver?'<span class="v">·待验证</span>':''}</span>`;}
function dotFor(sev){return sev?`<span class="dot ${esc(sev)}" title="质疑：${esc(sev)}"></span>`:'';}

// tabs
const loaded={stats:false,crit:false};
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  t.classList.add('on');
  ['ask','feed','stats','crit'].forEach(n=>$('#p-'+n).classList.toggle('hidden',n!==t.dataset.t));
  if(t.dataset.t==='stats'&&!loaded.stats){loadStats();}
  if(t.dataset.t==='crit'&&!loaded.crit){loadCrit();}
});

// ── 问答 ──
async function ask(){
  const q=$('#q').value.trim(); if(!q)return;
  const btn=$('#askBtn'); btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  $('#askOut').innerHTML='';
  try{
    const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({query:q,audit:$('#audit').checked})});
    renderAsk(await r.json());
  }catch(e){$('#askOut').innerHTML='<div class="warn">请求失败：'+esc(e)+'</div>';}
  btn.disabled=false; btn.textContent='查询';
}
function renderAsk(d){
  if(!d.found){
    $('#askOut').innerHTML=`<div class="empty"><div class="ic">🔍</div><p>知识库里没找到与「${esc(d.query)}」匹配的事实。<br>先入相关研报，或换更具体的实体/主线词。</p></div>`;
    return;
  }
  let h='';
  if(d.synthesis){
    h+=`<div class="sec"><h3>🤖 AI 综合</h3><div class="concl" style="white-space:pre-wrap;font-weight:500">${esc(d.synthesis)}</div></div>`;}
  if(d.conclusion){const c=d.conclusion;
    h+=`<div class="sec"><h3>结论</h3><div class="concl">${dotFor(c.doubt)}<div>${esc(c.claim)} ${badge(c.level,c.unverifiable)}</div></div></div>`;}
  if(d.evidence.length){
    h+='<div class="sec"><h3>证据链</h3>';
    d.evidence.forEach(f=>{h+=`<div class="ev"><span class="fid">F${f.idx}</span><div class="body">
      <div>${badge(f.level,f.unverifiable)} ${esc(f.claim)}</div>
      <div class="meta"><span>📚 来源 ${f.support} 篇</span><span>🔢 数字校验 ${f.verified}</span></div></div>${dotFor(f.doubt)}</div>`;});
    h+='</div>';
  }
  if(d.doubts.length){
    h+='<div class="sec"><h3>⚠ 质疑提示</h3><div class="doubtbox"><div class="lead">自动批判性体检 · 提醒别全信，不代表结论一定错</div>';
    d.doubts.forEach(x=>{h+=`<div class="drow">${dotFor(x.severity)}<span class="fid">F${x.idx}</span><div>${esc(x.message)}</div></div>`;});
    h+='</div></div>';
  }
  const cf=d.conflicts;
  if(cf.disputed.length||cf.invalidated.length){
    h+='<div class="sec"><h3>分歧 / 反证</h3>';
    cf.disputed.forEach(c=>h+=`<div class="ev"><span class="fid">争议</span><div class="body">${esc(c)}</div></div>`);
    cf.invalidated.forEach(c=>h+=`<div class="ev"><span class="fid">${esc(c.status)}</span><div class="body">${esc(c.claim)}</div></div>`);
    h+='</div>';
  }
  if(d.followup.length){
    h+='<div class="sec"><h3>后续验证（待坐实）</h3>';
    d.followup.forEach(c=>h+=`<span class="chip">⏳ ${esc(c)}</span>`);
    h+='</div>';
  }
  if(d.neighbors.length){
    h+='<div class="sec"><h3>结构关联</h3>';
    d.neighbors.forEach(n=>h+=`<span class="chip">${esc(n.rel)} → ${esc(n.name)}</span>`);
    h+='</div>';
  }
  if(d.sources.length){
    h+='<div class="sec"><h3>引用来源</h3><div class="srcs">';
    d.sources.forEach(s=>h+=`<span class="src">card:${esc(s)}</span>`);
    h+='</div></div>';
  }
  if(d.warnings.length){d.warnings.forEach(w=>h+=`<div class="warn">⚠ ${esc(w)}</div>`);}
  $('#askOut').innerHTML=h;
}
$('#askBtn').onclick=ask;
$('#q').addEventListener('keydown',e=>{if(e.key==='Enter')ask();});

// ── 投喂 ──
async function feed(){
  const text=$('#frag').value.trim(); if(!text){return;}
  const btn=$('#feedBtn'); btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  try{
    const r=await fetch('/api/feed',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,watch:$('#watch').value})});
    const d=await r.json();
    if(!d.ok){$('#feedOut').innerHTML=`<div class="warn">${esc(d.msg||'失败')}</div>`;}
    else{$('#feedOut').innerHTML=`<div class="sec"><h3>投喂回执</h3><div class="grid">
      <div class="stat"><div class="k">碎片总数</div><div class="n">${d.total}</div></div>
      <div class="stat"><div class="k">命中入库</div><div class="n" style="color:var(--A)">${d.kept}</div><div class="x">提到关注标的</div></div>
      <div class="stat"><div class="k">噪声冷存</div><div class="n" style="color:var(--D)">${d.cold}</div><div class="x">留底不入库</div></div>
      <div class="stat"><div class="k">关注标的池</div><div class="n">${d.watch}</div></div>
    </div><div class="hint">默认 D 级、隔离；被 B+ 信源印证后才升级为正经证据。</div></div>`;
      loaded.stats=false;}
  }catch(e){$('#feedOut').innerHTML='<div class="warn">请求失败：'+esc(e)+'</div>';}
  btn.disabled=false; btn.textContent='投喂';
}
$('#feedBtn').onclick=feed;

// ── 概览 ──
function lvbar(dist){
  const tot=Object.values(dist).reduce((a,b)=>a+b,0)||1;
  const col={A:'var(--A)',B:'var(--B)',C:'var(--C)',D:'var(--D)'};
  let s='';for(const k of['A','B','C','D']){const w=(dist[k]||0)/tot*100;if(w>0)s+=`<i style="width:${w}%;background:${col[k]}" title="${k}级 ${dist[k]}"></i>`;}
  return `<div class="lvbar">${s}</div>`;
}
async function loadStats(){
  loaded.stats=true;
  const r=await fetch('/api/stats'); const d=await r.json();
  const f=d.facts||{},e=d.entities||{},st=d.structure||{},se=d.sentiment||{};
  const dist=f.by_level||{}, active=(f.by_status||{}).active??0;
  $('#statsOut').innerHTML=`<div class="grid">
    <div class="stat"><div class="k">⏳ 时序事实</div><div class="n">${f.total||0}</div><div class="x">active ${active}</div>${lvbar(dist)}</div>
    <div class="stat"><div class="k">🕸 结构关系</div><div class="n">${st.total||0}</div><div class="x">低置信 ${st.low_confidence||0}</div></div>
    <div class="stat"><div class="k">🏷 实体注册</div><div class="n">${e.entities||0}</div><div class="x">待补 code ${e.pending_stocks||0}</div></div>
    <div class="stat"><div class="k">💬 舆情碎片</div><div class="n">${se.items||0}</div><div class="x">留底 ${se.raw_total||0} · 升级 ${se.promoted||0}</div></div>
  </div>`;
  updatePills(f,e);
}
async function reingest(){
  const btn=$('#reingestBtn'); btn.disabled=true; btn.innerHTML='<span class="spin" style="border-color:rgba(124,58,237,.3);border-top-color:var(--accent)"></span> 摄入中…';
  try{
    const r=await fetch('/api/ingest',{method:'POST'}); const d=await r.json();
    if(d.error){alert('失败：'+d.error);}
    else{loaded.stats=false;loadStats();
      $('#statsOut').insertAdjacentHTML('afterbegin',
       `<div class="warn" style="background:#eafaef;border-color:#bdebca;color:#1a7a37">✓ 已重摄入：卡片 ${d.cards} · findings ${d.findings} · 硬事实 ${d.hard_facts} · 量化 ${d.quant_facts} · 质疑 ${d.doubts} 条</div>`);}
  }catch(e){alert('请求失败：'+e);}
  btn.disabled=false; btn.textContent='⟳ 重摄入 report_lab 卡片';
}
$('#reingestBtn').onclick=reingest;

// ── 质疑榜 ──
async function loadCrit(){
  loaded.crit=true;
  const r=await fetch('/api/critique'); const d=await r.json();
  if(!d.items||!d.items.length){$('#critOut').innerHTML='<div class="empty"><div class="ic">✓</div><p>暂无带质疑标记的结论。<br>（量化语料里硬事实少；灌入行业/公司研报后此榜更有料）</p></div>';return;}
  let h=`<p class="muted">共 ${d.total} 条结论带质疑，按严重度排序取前 ${d.items.length}：</p>`;
  d.items.forEach(it=>{
    h+=`<div class="ev"><div class="body"><div>${badge(it.level,false)} ${esc(it.claim)}</div>`;
    it.flags.forEach(fl=>h+=`<div class="drow">${dotFor(fl.severity)}<div>${esc(fl.message)}</div></div>`);
    h+='</div></div>';
  });
  $('#critOut').innerHTML=h;
}

// pills in header
function updatePills(f,e){
  f=f||{};e=e||{};
  $('#pills').innerHTML=
    `<span class="pill">⏳ 时序事实 <b>${f.total||0}</b></span>`+
    `<span class="pill">🏷 实体 <b>${e.entities||0}</b></span>`+
    `<span class="pill">🟢 标准库 · 离线可复现</span>`;
}
loadStats();
</script>
</body>
</html>"""

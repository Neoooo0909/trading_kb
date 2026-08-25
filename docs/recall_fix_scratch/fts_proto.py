"""FTS5 bigram+BM25 原型:独立 db,只读 facts.db,实测建库耗时/体积/查询延迟与召回质量。"""
import sqlite3, time, sys, os, re
sys.path.insert(0, "src")
from trading_kb.models import content_grams
S = os.path.dirname(os.path.abspath(__file__))
FTS = f"{S}/facts_fts_proto.db"
if os.path.exists(FTS): os.remove(FTS)
src = sqlite3.connect("file:data/facts.db?mode=ro", uri=True)
dst = sqlite3.connect(FTS)
dst.executescript("""
PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
CREATE TABLE fts_map(id INTEGER PRIMARY KEY, fact_id TEXT UNIQUE);
CREATE VIRTUAL TABLE facts_fts USING fts5(grams, content='', tokenize='unicode61');
""")
def toks(claim, obj, subj):
    g = content_grams(f"{claim or ''} {obj or ''} {subj or ''}")
    return " ".join(sorted(g))
t0=time.time(); n=0
cur = src.execute("SELECT fact_id, claim, object, subject FROM facts WHERE status IN ('active','disputed')")
batch=[]
while True:
    rows = cur.fetchmany(20000)
    if not rows: break
    dst.execute("BEGIN")
    for fid, c, o, s in rows:
        rid = dst.execute("INSERT INTO fts_map(fact_id) VALUES(?)", (fid,)).lastrowid
        dst.execute("INSERT INTO facts_fts(rowid, grams) VALUES(?,?)", (rid, toks(c,o,s)))
    dst.execute("COMMIT"); n+=len(rows)
    if n % 200000 == 0: print(f"  {n} rows {time.time()-t0:.0f}s", flush=True)
dst.execute("INSERT INTO facts_fts(facts_fts) VALUES('optimize')")
print(f"BUILD {n} rows in {time.time()-t0:.0f}s, size {os.path.getsize(FTS)/1e6:.0f}MB", flush=True)
def q(text, kw, limit=400):
    words=[t for t in re.split(r"[\s,，、;；。]+", text) if 2<=len(t)<=40]
    g=[x for x in content_grams(text) if len(x)>=2]
    terms=sorted(set(g))
    match=" OR ".join(f'"{t}"' for t in terms)
    t0=time.time()
    rows=dst.execute("SELECT m.fact_id, bm25(facts_fts) AS s FROM facts_fts JOIN fts_map m ON m.id=facts_fts.rowid WHERE facts_fts MATCH ? ORDER BY s LIMIT ?", (match, limit)).fetchall()
    dt=time.time()-t0
    fids=[r[0] for r in rows]
    claims={}
    for i in range(0,len(fids),500):
        ch=fids[i:i+500]
        for fid,c,lv,va in src.execute(f"SELECT fact_id,claim,evidence_level,valid_at FROM facts WHERE fact_id IN ({','.join('?'*len(ch))})", ch): claims[fid]=(c,lv,va)
    hit=[f for f in fids if kw in (claims.get(f,('',))[0] or '') and '全'+kw not in (claims.get(f,('',))[0] or '')]
    print(f"\nQUERY {text!r}: {len(rows)} rows in {dt:.2f}s, 真·{kw}命中 {len(hit)}; 命中位置 {[fids.index(f)+1 for f in hit][:30]}")
    for f,s in rows[:12]:
        c,lv,va=claims.get(f,('',None,None)); print(f"  {s:.2f} {lv} {va} | {(c or '')[:70]}")
for text,kw in [("球硅 HBM 先进封装 需求","球硅"),("燃气轮机 数据中心 自备电源 受益标的","燃气轮机"),("银轮股份","银轮"),("精智达 存储测试设备 进展","精智达")]:
    q(text,kw)

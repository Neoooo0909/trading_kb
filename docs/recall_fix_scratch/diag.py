import sys, sqlite3, time
sys.path.insert(0, "src")
from datetime import date as _Date
from trading_kb.ask import AskEngine, _merge_facts, _diversify_by_kind, _recency, _content_grams, _LEVEL_RANK, _is_security
from trading_kb import config
q = sys.argv[1] if len(sys.argv) > 1 else "球硅 HBM 先进封装 需求"
kw = sys.argv[2] if len(sys.argv) > 2 else "球硅"
import inspect
# construct asker the way cli does
from trading_kb import ask as A
src = inspect.getsource(A)
from trading_kb.cli import EntityRegistry, FactsStore, StructureStore
asker = AskEngine(EntityRegistry(config.ENTITY_DB), FactsStore(config.FACTS_DB), StructureStore(config.STRUCTURE_DB))
def run(q, kw):
    print('\n########', q, '/', kw)
    cid = asker._locate_entity(q)
    print("cid=", cid, "want_sem=", cid is None or not _is_security(cid))
    t=time.time()
    like = asker.facts.search(q, limit=400)
    sem = asker._semantic_recall(q, top_k=120)
    print("like", len(like), "sem", len(sem), f"{time.time()-t:.1f}s")
    def is_hit(f): 
        c=f.get("claim") or ""; return kw in c and "全"+kw not in c
    print("like真命中", sum(map(is_hit, like)), "sem真命中", sum(map(is_hit, sem)))
    pool=[]
    if cid:
        pool += asker.facts.query(canonical_id=cid, include_invalidated=False, limit=120)
        pool = _merge_facts(pool, asker.facts.query(canonical_id=cid, limit=60, order="recent"))
        pool = _merge_facts(pool, asker.facts.query(canonical_id=cid, levels=["C","D"], limit=60, order="recent"))
    pool = _merge_facts(pool, like); pool=_merge_facts(pool, sem)
    print("pool", len(pool))
    qg=_content_grams(q); nq=max(len(qg),1); today=_Date.today().toordinal()
    semsc = asker._semantic_scores(q, pool)
    import statistics
    print("sem score dist: min/median/max", min(semsc.values()), statistics.median(semsc.values()), max(semsc.values()))
    rows=[]
    for f in pool:
        fcid=f.get("canonical_id"); ent=1.0 if (cid and fcid==cid) else 0.0
        fg=_content_grams(f"{f.get('claim','')} {f.get('object','')}")
        rel=len(qg&fg)/nq; ss=semsc.get(f["fact_id"],0.0)
        relevance=1.5*rel+2*ent+2*ss
        if relevance<=0: continue
        level=_LEVEL_RANK.get(f.get("evidence_level"),1)/4.0; rec=_recency(f.get("valid_at"),today); sup=min(f.get("support_count") or 0,10)/10
        score=relevance*2+level+rec+sup*0.5
        rows.append((score,rel,ss,ent,level,rec,sup,f))
    rows.sort(key=lambda r:-r[0])
    print("\n== top 12 by score ==")
    for i,r in enumerate(rows[:12]):
        f=r[7]; print(f"#{i+1} sc={r[0]:.2f} rel={r[1]:.2f} sem={r[2]:.2f} lv={r[4]:.2f} rec={r[5]:.2f} {f.get('evidence_level')} {f.get('source_kind')} {f.get('valid_at')} | {(f.get('claim') or '')[:60]}")
    print("\n== 真命中在排序里的位置 ==")
    for i,r in enumerate(rows):
        if is_hit(r[7]):
            f=r[7]; print(f"#{i+1} sc={r[0]:.2f} rel={r[1]:.2f} sem={r[2]:.2f} lv={r[4]:.2f} rec={r[5]:.2f} {f.get('evidence_level')} {f.get('source_kind')} {f.get('valid_at')} | {(f.get('claim') or '')[:60]}")
    div=_diversify_by_kind([(r[0],r[7]) for r in rows])
    print("\n== diversify 后前 12 ==")
    for i,f in enumerate(div[:12]):
        print(f"#{i+1} {f.get('evidence_level')} {f.get('source_kind')} {'HIT' if is_hit(f) else '   '} | {(f.get('claim') or '')[:60]}")
    print("div 后真命中位置:", [i+1 for i,f in enumerate(div) if is_hit(f)])
for q,kw in [('球硅 HBM 先进封装 需求','球硅'),('燃气轮机 数据中心 自备电源 受益标的','燃气轮机')]:
    run(q,kw)

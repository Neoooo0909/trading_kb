"""抽样归因：对各 lane 卡片随机抽样，跑规则分流，统计 background 占比并按启发式分桶，
导出 500 条 background 供 LLM 二次分档。"""
import json, random, re, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path.home()/"trading_kb/src"))
from trading_kb.classify import classify_finding, _has_hard_number, _YEAR_RE, _BOILERPLATE, _FINANCIAL_KW, _match_hard_predicate
from trading_kb.report_lab_adapter import card_to_findings, card_entities
random.seed(20260826)
LANES = {
  "zsxq_posts": (Path.home()/"ZSXQ/kb_adapter/cards", 1500),
  "ima":        (Path.home()/"ZSXQ/kb_adapter/cards_ima", 800),
  "zsxq_research": (Path.home()/"ZSXQ/kb_adapter/cards_zsxq_research", 800),
  "report_lab": (Path.home()/"report_lab/cards", 300),
}
OUT = Path(sys.argv[1]) if len(sys.argv)>1 else Path("bg_sample.jsonl")
stats = collections.defaultdict(collections.Counter)
bg_rows = []
for lane,(d,n) in LANES.items():
    files = sorted(d.glob("*.json"))
    random.shuffle(files); files = files[:n]
    for fp in files:
        try: card = json.loads(fp.read_text(encoding="utf-8"))
        except Exception: continue
        if not isinstance(card, dict): continue
        stock_names = {e["name"] for e in card_entities(card) if e.get("kind") in ("stock","company")}
        for f in card_to_findings(card):
            f.source_kind = card.get("source_kind", f.source_kind)
            cat = classify_finding(f, llm=None)
            stats[lane][cat]+=1
            if cat!="background": continue
            raw = f"{f.claim} {f.evidence}"
            ents = [e for e in f.entities if e]
            has_stock = any(e in stock_names for e in ents) or any(s in f.claim for s in stock_names if s)
            hn = _has_hard_number(raw); yr = bool(_YEAR_RE.search(raw)); nums = bool(f.numbers)
            bp = bool(_BOILERPLATE.search(f.claim or ""))
            if bp: b="BOILERPLATE"
            elif has_stock and (hn or nums): b="STOCK_NUM"
            elif has_stock: b="STOCK_QUAL"
            elif ents and (hn or nums): b="ENT_NUM"
            elif ents: b="ENT_QUAL"
            elif (hn or nums): b="NOENT_NUM"
            else: b="NOENT_QUAL"
            stats[lane+"|bg_bucket"][b]+=1
            bg_rows.append({"lane":lane,"card":card.get("id"),"type":card.get("type"),"date":card.get("date"),
                "claim":f.claim,"evidence":f.evidence[:300],"entities":ents,"numbers":[n.get("value") for n in f.numbers if isinstance(n,dict)],
                "bucket":b,"has_stock":has_stock,"hard_num":hn,"year":yr,"fnum":nums,"claim_len":len(f.claim)})
for k in sorted(stats):
    c = stats[k]; tot=sum(c.values())
    print(f"{k:28s} total={tot:6d} ", {x:f"{v} ({v/tot:.1%})" for x,v in c.most_common()})
random.shuffle(bg_rows)
with OUT.open("w",encoding="utf-8") as w:
    for r in bg_rows: w.write(json.dumps(r,ensure_ascii=False)+"\n")
print("bg rows:", len(bg_rows), "->", OUT)

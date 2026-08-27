"""生产库完整性体检(只读):状态分布 / 同句多行重复 / 回填引入的重复 / 实体引用完整性 / 未知主体 / sources 一致性。
用法: PYTHONPATH=src python3 docs/background_fix_scratch/integrity_check.py"""
import sqlite3, json, collections, time, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "trading_kb" / "src"))
from trading_kb.models import _normalize
t = time.time()
root = Path.home() / "trading_kb" / "data"
c = sqlite3.connect(f"file:{root/'facts.db'}?mode=ro", uri=True); c.row_factory = sqlite3.Row
e = sqlite3.connect(f"file:{root/'entities.db'}?mode=ro", uri=True)
print("状态分布:", dict(c.execute("select status,count(*) from facts group by 1").fetchall()))
print("类别×成色(active):", [(r[0], r[1], r[2]) for r in c.execute(
    "select category,evidence_level,count(*) from facts where status='active' group by 1,2 order by 3 desc").fetchall()][:12])
rows = c.execute("select fact_id,claim,canonical_id,predicate,category,evidence_level,sources,support_count,extra "
                 "from facts where status in ('active','disputed')").fetchall()
print("active+disputed 行数:", len(rows), f"({time.time()-t:.0f}s)")
groups = collections.defaultdict(list)
for r in rows:
    groups[_normalize(r["claim"] or "")].append(r)
multi = {k: v for k, v in groups.items() if len(v) > 1}
n_multi = sum(len(v) for v in multi.values())
same_doc = diff_subject = diff_pred = diff_cat = 0
for k, v in multi.items():
    srcs = [set(json.loads(r["sources"] or "[]")) for r in v]
    if any(srcs[i] & srcs[j] for i in range(len(v)) for j in range(i + 1, len(v))):
        same_doc += 1
    if len({r["canonical_id"] for r in v}) > 1: diff_subject += 1
    if len({r["predicate"] for r in v}) > 1: diff_pred += 1
    if len({r["category"] for r in v}) > 1: diff_cat += 1
print(f"[重复] 同句多行的组: {len(multi)} 组, 涉及 {n_multi} 行 ({n_multi/len(rows):.1%})")
print(f"  其中 同一来源文档内(真重复): {same_doc} 组 | 主体不同: {diff_subject} | 谓词不同: {diff_pred} | 类别不同: {diff_cat}")
ents = {r[0]: r[1] for r in e.execute("select canonical_id, merged_into from entities")}
missing = sum(1 for r in rows if r["canonical_id"] not in ents)
merged = sum(1 for r in rows if ents.get(r["canonical_id"]))
print(f"[实体] 引用不存在实体: {missing} | 引用已合并实体: {merged}")
unk = sum(1 for r in rows if "未知主体" in (r["canonical_id"] or ""))
print(f"[主体] 未知主体事实: {unk} ({unk/len(rows):.2%})")
bad = sum(1 for r in rows if (r["support_count"] or 0) != max(len(json.loads(r["sources"] or "[]")), 1))
print(f"[来源] support_count≠len(sources): {bad}")
try:
    print("[归档] facts_merged_archive:", c.execute("select role,count(*) from facts_merged_archive group by 1").fetchall())
except sqlite3.OperationalError:
    print("[归档] facts_merged_archive: (无表)")
print("[留痕] background_log:", c.execute("select count(*) from background_log").fetchone()[0])
print(f"体检耗时 {time.time()-t:.0f}s")

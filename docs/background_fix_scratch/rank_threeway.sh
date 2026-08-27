#!/bin/zsh
# 同一索引上的三方排位对照:current(现状) / no_v3(剔除回填行≈回填前) / pre_dedup(dedup 前备份+回放 valid_at 修复)。
# 变体库放临时目录,用 TKB_DATA_DIR 指过去;向量库/实体库/结构库/舆情库用软链接共享同一份索引。
set -u
cd ~/trading_kb
D=docs/background_fix_scratch; TMP=${TMPDIR_VARIANT:-/private/tmp/claude-501/tkb_variant}; LOG=$D/rank_threeway.log
ts(){ date "+%m-%d %H:%M:%S"; }
stage(){ grep -o ">> \[[①②③]/3\][^（(]*" ~/kb_sync/sync_all.log | tail -1; }
echo "[$(ts)] kbsync 阶段: $(stage) | vectors=$(python3 -c "import sqlite3;print(sqlite3.connect('file:data/vectors_bge.db?mode=ro',uri=True).execute('select count(*) from vectors').fetchone()[0])")" >> $LOG
echo "[$(ts)] ---- current" >> $LOG
PYTHONPATH=src python3 $D/rank_snapshot.py > $D/rank_3way_current.md 2>>$LOG
mkvariant(){  # $1=name $2=source db
  rm -rf $TMP/$1; mkdir -p $TMP/$1
  python3 - "$2" "$TMP/$1/facts.db" <<'PY'
import sqlite3,sys
src=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close()
PY
  for f in entities.db structure.db sentiment.db vectors_bge.db vectors_bge.db.mat.npy vectors_bge.db.ids.json; do ln -s ~/trading_kb/data/$f $TMP/$1/$f; done
}
echo "[$(ts)] ---- no_v3(剔除 rule_version=v3 回填行)" >> $LOG
mkvariant no_v3 data/facts.db
python3 - $TMP/no_v3/facts.db $D/backfill_v3_fact_ids.txt <<'PY' >> $LOG 2>&1
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); ids=[(l.strip(),) for l in open(sys.argv[2]) if l.strip()]
c.execute("CREATE TEMP TABLE v3(id TEXT PRIMARY KEY)"); c.executemany("INSERT OR IGNORE INTO v3 VALUES(?)",ids)
n=c.execute("DELETE FROM facts WHERE fact_id IN (SELECT id FROM v3)").rowcount; c.commit()
print("no_v3: 删除", n, "行, active", c.execute("select count(*) from facts where status='active'").fetchone()[0])
PY
TKB_DATA_DIR=$TMP/no_v3 PYTHONPATH=src python3 $D/rank_snapshot.py > $D/rank_3way_no_v3.md 2>>$LOG
rm -rf $TMP/no_v3
echo "[$(ts)] ---- pre_dedup(dedup 前备份 + 回放 valid_at 修复)" >> $LOG
mkvariant pre_dedup .backup/facts.db.bak_dedup_1787747923
python3 - $TMP/pre_dedup/facts.db data/facts.db <<'PY' >> $LOG 2>&1
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); live=sqlite3.connect(f"file:{sys.argv[2]}?mode=ro",uri=True)
rows=live.execute("select fact_id,new_valid_at from valid_at_backfill_log").fetchall()
c.executemany("UPDATE facts SET valid_at=? WHERE fact_id=? AND (valid_at='' OR valid_at IS NULL)",[(n,f) for f,n in rows]); c.commit()
print("pre_dedup: 回放 valid_at", len(rows), "行, active", c.execute("select count(*) from facts where status='active'").fetchone()[0],
      "空 valid_at", c.execute("select count(*) from facts where status='active' and (valid_at='' or valid_at is null)").fetchone()[0])
PY
TKB_DATA_DIR=$TMP/pre_dedup PYTHONPATH=src python3 $D/rank_snapshot.py > $D/rank_3way_pre_dedup.md 2>>$LOG
rm -rf $TMP/pre_dedup
echo "[$(ts)] kbsync 阶段: $(stage) | vectors=$(python3 -c "import sqlite3;print(sqlite3.connect('file:data/vectors_bge.db?mode=ro',uri=True).execute('select count(*) from vectors').fetchone()[0])")" >> $LOG
echo "[$(ts)] ======== 三方对照完成 ========" >> $LOG

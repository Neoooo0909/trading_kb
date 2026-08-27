#!/bin/zsh
# 等回填编排结束 → dedup --apply(deadline 00:30) → 清孤儿向量 → FTS 对账 → 体检 → 排位对照。用户 2026-08-26 晚拍板。
set -u
cd ~/trading_kb
LOG=docs/background_fix_scratch/dedup_apply.log
ts(){ date "+%m-%d %H:%M:%S"; }
echo "[$(ts)] 等待回填编排结束…" >> $LOG
while pgrep -f "[r]un_full_backfill.sh" >/dev/null; do sleep 60; done
echo "[$(ts)] 回填编排已结束" >> $LOG
NOW=$(date +%Y%m%d%H%M)
if [[ "$NOW" > "202608270015" && "$NOW" < "202608270900" ]]; then
  echo "[$(ts)] 已过 00:15,推迟到明天白天手动跑 dedup(避开 01:00 kbsync)" >> $LOG; exit 3
fi
echo "[$(ts)] ---- dedup --apply" >> $LOG
python3 scripts/dedup_same_claim.py --apply --deadline "2026-08-27 00:30" --sample 5 >> $LOG 2>&1
echo "[$(ts)] dedup rc=$?" >> $LOG
echo "[$(ts)] ---- semantic build(清孤儿向量)" >> $LOG
./tkb semantic build >> $LOG 2>&1; echo "[$(ts)] semantic rc=$?" >> $LOG
./tkb fts status >> $LOG 2>&1
echo "[$(ts)] ---- 体检" >> $LOG
PYTHONPATH=src python3 docs/background_fix_scratch/integrity_check.py >> $LOG 2>&1
echo "[$(ts)] ---- 排位对照(合并后)" >> $LOG
PYTHONPATH=src python3 docs/background_fix_scratch/rank_snapshot.py > docs/background_fix_scratch/rank_after_dedup_warm.md 2>>$LOG
PYTHONPATH=src python3 docs/background_fix_scratch/rank_snapshot.py > docs/background_fix_scratch/rank_after_dedup.md 2>>$LOG
echo "[$(ts)] ======== dedup 流程全部完成 ========" >> $LOG

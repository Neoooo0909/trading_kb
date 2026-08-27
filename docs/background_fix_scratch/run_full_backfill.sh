#!/bin/zsh
# 全量回填编排(2026-08-26 用户拍板):四目录串行 → 向量增量 → FTS 对账 → 排位/冷启动对照。幂等可续跑。
set -u
cd ~/trading_kb
LOG=docs/background_fix_scratch/backfill_full.log
DL="2026-08-27 00:30"
ts(){ date "+%m-%d %H:%M:%S"; }
echo "[$(ts)] ======== 全量回填开始 (deadline $DL) ========" >> $LOG
for spec in "$HOME/ZSXQ/kb_adapter/cards_zsxq_research|social_research" "$HOME/ZSXQ/kb_adapter/cards_ima|social_research" "$HOME/report_lab/cards|"; do
  d="${spec%%|*}"; kind="${spec##*|}"
  echo "[$(ts)] ---- lane $d (default-kind='${kind}')" >> $LOG
  python3 scripts/backfill_background.py --dirs "$d" --apply --no-backup --deadline "$DL" --default-kind "$kind" >> $LOG 2>&1
  echo "[$(ts)] lane rc=$?" >> $LOG
  if [[ "$(date +%Y%m%d%H%M)" > "202608270030" ]]; then echo "[$(ts)] 过 deadline,停" >> $LOG; exit 3; fi
done
echo "[$(ts)] ---- semantic build" >> $LOG
./tkb semantic build >> $LOG 2>&1; echo "[$(ts)] semantic rc=$?" >> $LOG
echo "[$(ts)] ---- fts status" >> $LOG
./tkb fts status >> $LOG 2>&1
echo "[$(ts)] ---- rank snapshot (第一遍触发 .npy 缓存重建, 第二遍计时)" >> $LOG
PYTHONPATH=src python3 docs/background_fix_scratch/rank_snapshot.py > docs/background_fix_scratch/rank_after_warm.md 2>>$LOG
PYTHONPATH=src python3 docs/background_fix_scratch/rank_snapshot.py > docs/background_fix_scratch/rank_after.md 2>>$LOG
echo "[$(ts)] ======== 全部完成 ========" >> $LOG

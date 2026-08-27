# valid_at 空值修复方案（2026-08-26，xiaoqing-90 复核 xiaoqing-dc 的报告）

> 状态：用户 2026-08-26 夜批准（经 xiaoqing-dc 转达「按他的结论来调整」）。**代码闸口已落地（`dates.py` / `facts_store` ingested_at+日期清洗 / `ima_to_card` 统一日期解析，222 tests 绿）；存量 `--apply` 定于 08-27 白天执行**（避开 01:00 kbsync）。
> 干跑脚本与逐卡建议在 `/private/tmp/claude-501/-Users-xiaoqing/abda0701-…/scratchpad/`（`valid_at_audit.py` / `l1_consistency.py` / `zsxq_post_date.py` / `date_recovery_dryrun.py` → `proposals.jsonl`），全部只读。

## 1. 复核结论（对 dc 报告逐条）

| dc 的判断 | 复核结果 |
|---|---|
| 缺失在卡片层，不是入库丢失 | **成立**。空 valid_at 的 active 事实 616,878（合并前，32.5%；合并后 597,024 / 1,842,742 = 32.4%），逐条回溯到来源卡片：**卡片 date 为空 611,782（99.2%）**、卡片已被 ima 去重删除找不到 5,096（0.8%）、**"卡片有日期但事实没有"= 0**。 |
| 根因是"LLM 抽卡没抽出日期" | **不准确**。ima/zsxq_research 卡片的 `date` 从来不是 LLM 抽的，是 `ima_to_card._guess_date(文件名)`，正则 `DATE_RE` 只认 8 位 `YYYYMMDD`（`(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})`），文件名没有 8 位日期就留空。所以 6 位 `YYMMDD`、纯标题文件名全空。 |
| 影响面只在 cards_ima（10,272 / 30.1%） | **范围漏了一半**。`cards_zsxq_research` 22,542 张里 **15,245（67.6%）空**（其中 12,798 来自"每日调研纪要"星球，纪要文件名几乎不带日期）。事实层按 lane：zsxq_research 347,812（56%）> ima 263,965（43%）> social 5。社媒帖 `cards` 70,557 张只有 1 张空，report_lab 0。 |
| view 事实的无日期是否同源 | **同源**。109,900 条 view 全部是 card_no_date，无一条来自回填逻辑；25,367 条 `structure/social_research` 也不是社媒——是 `_ingest_cross_market` 把研报卡的跨市场边写成 rationale 事实时 source_kind 硬编码 `social_research`、valid_at=卡片 date，同一批空日期研报卡。修卡片一次解决三边。 |
| 用入库日期填 valid_at 是错的 | **同意**，理由同 dc；另外 facts 表无 `ingested_at`，也无法这么做。 |
| L1（PDF CreationDate）最大未验证假设 | **已验证，可信但要加两道闸**（§2）。 |
| 格式非法 24 条 / 未来 3 条 | 口径不同：`YYYY-MM-DD` 形状但日历非法的有 **133 条**（`2025-00-82`×33、`2026-84-17`×17、`2020-50-40`×16…），133 条**全部与来源卡片 date 同值**——是 `_guess_date` 加日历校验之前留下的（其 docstring 记的"曾污染 39 条"只修了代码没修存量）；未来日期 3 条（`2026-11-14`）。 |

## 2. 三条恢复路径的校准（全量扫描，非抽样）

**L0 · 星球帖子 `create_time`（新增，主力）**
- `~/ZSXQ/download/*/帖子/posts.jsonl` 每帖带 `files[].name`；`cards_zsxq_research` 的卡 id = `"ima_"+sha1(canonical 路径)[:12]`，由 `zsxq_research_canonical.txt` 反查得到路径 → 星球分组 → **同组同名**匹配帖子。
- 匹配率：15,245 张空日期卡中 **14,917 张（97.8%）**；ima 空日期卡也有 4,746 张按裸文件名能匹配到（同一批研报既进了 ima 库也在星球发过）。
- 校准（7,249 张"文件名带日期"的卡拿帖日期对文件名日期）：**=0 天 57.6%、≤3 天 94.5%、≤14 天 99.6%**；>60 天仅 5 张（老研报被重新转发，如 2025-11 的中泰谷歌链专题在 2026-04 再发）。
- 歧义：同一文件名在同组多帖出现 385 张，其中 338 张各帖极差 ≤3 天（取最早帖）；干跑规则"极差 >3 天判 AMBIG 不填"实际命中 **0** 张。
- 分布：2026-01~08 逐月 169~3,704，无单日堆积（最多一天 320 张）。

**L1 · PDF `/CreationDate`（ima 卡主力，dc 的路径）**
- dc 要的 200 张双来源一致性：PDF 部分 173 张，**=0 天 57%、≤7 天 98%**，>180 天 2 张（花旗 `新易盛 260201.pdf` CreationDate=2023-09-14）。
- 更大样本：3,772 张"已有日期卡" vs 其 PDF CreationDate：**=0 天 65%、≤7 天 99%**，CreationDate 偏晚 1~7 天（转档），>180 天 1 张。
- **两类失效**：① **模板日期**——花旗研报 PDF 的 CreationDate 全是 2023-09-14（模板创建日）；② **批量归档日**——SemiAnalysis 文章被人在 2023-09-14~16 三天内批量打印成 PDF（177 张，内容实际是 2022~2023 各月）。二者都表现为"同一个 CreationDate 挤了几十个不相干的文件"。
- 建议闸门：某个 CreationDate 值在全库出现 ≥20 次 → 视为模板/归档日，**不采用**（能用 ModDate 且 ModDate 与 CreationDate 相差 >30 天时取 ModDate，否则留空）。按干跑数据这一闸约剔 400 张，全部是 >1 年的老文章，对 `_recency` 本来就是 0，不亏。
- 其余 CreationDate 分布 445 个唯一日、无单日 >2%。

**L2 · 文件名 6 位 `YYMMDD`（dc 提的补充）**
- 2,361 张 ima 卡靠它恢复（国内券商纪要 `…交流250429` 惯例）。必须日历合法且 ≤ 今天+1，否则重蹈 `2026-84-17` 覆辙。

**优先级**：L0 > L1 > L2；多源同时存在且相差 >30 天的 **25 张**不填、列清单人工看（样例：`存储调研.pdf` 帖 2026-06-24 vs CreationDate 2025-09-24）。

## 3. 恢复量（干跑，proposals.jsonl）

| | 卡 | 事实（active，合并后） |
|---|---|---|
| 空日期总量 | 25,517 | 597,024 |
| 可恢复 | **24,493（96.0%）**：L0 19,663 / L1 2,374 / L1_moddate 95 / L2 2,361 | **563,109（94.3%）** |
| 加 L1 ≥20 次闸门后 | 约 24,090（94.4%） | 约 55 万 |
| 仍留空 | 1,024 张（非 PDF、外接盘/星球都找不到、L2 无日期） | 33,915 + 5,096 无卡 |

**正式脚本干跑 #2（2026-08-27 00:32，`scripts/backfill_valid_at.py`，PDF 元数据已缓存 `data/pdf_date_cache.json`）**：待处理卡 25,531（含 14 张日历非法）；可补 **24,304**（zsxq_post 19,648 / pdf_creation 2,242 / pdf_mod 50 / filename 2,364），冲突 25、无来源 1,202；黑名单按"独立日期一致率 <50% 或无独立样本且 >1 年"判定后恰为 3 天 `2023-09-14/15/16`（第一版"≥20 次即拉黑"误杀了 175 个正常发布日，已改）；事实层 **补 560,246 行、清洗非法 97 行**。

## 4. 修复方案

### 4.1 修在哪（结论：卡片 + 事实两边都改，但**不重跑入库**）
- **卡片**：`cards_ima/*.json`、`cards_zsxq_research/*.json` 写入 `date`，并加 `date_source: zsxq_post | pdf_creation | pdf_mod | filename_yymmdd`。
- **事实**：直接 `UPDATE facts SET valid_at=?, extra=json_set(extra,'$.valid_at_source',?) WHERE fact_id=? AND (valid_at='' OR valid_at IS NULL)`，覆盖**全部状态**（superseded 行也补，避免以后合并路径 `min(valid_at)` 取到空）。fact_id 清单由一次全表扫描按 `sources ∋ doc_id` 得到（1.8M 行 ~1 min），再按主键 5,000 行一事务更新，全程 <15 min。
- **明确不要走 `ingest_kb_cards.py` 重入库**：① 改卡片 → mtime 变 → `.ingested.txt` 判为新卡 → 25k 卡全部重入；② `facts.upsert` 合并路径 `status='active'` 会**复活 superseded/invalidated 行**（backfill 文档已记的坑）；③ 它没有 (doc, claim) 判重，会把今晚刚合并掉的同文同句重复再造一遍。写卡片后用 `os.utime` 还原原 mtime（或同步改 `.ingested.txt` 的 `name:mtime` 键），让 01:00 kbsync 不重入。
- 133 条日历非法 + 3 条未来日期：同一脚本按新规则重算（多数文件名里有 6/8 位可用日期），算不出就置空；不单独立项。
- 副作用检查：FTS 文本不含 valid_at、向量文本不含 valid_at、structure.db 无日期字段 → **不需要重建任何索引**。
- 可回滚：动手前备份 facts.db；新建表 `valid_at_backfill_log(fact_id, old_valid_at, new_valid_at, source, at)`，`--undo` 按它还原。

### 4.2 入库闸口（防再犯，三处小改）
1. `ima_to_card._guess_date`：`DATE_RE` 补 6 位 `YYMMDD` 与中文年月日；文件名无日期时依次试 星球帖子索引（zsxq_research lane 有组信息）→ PDF CreationDate（带 ≥20 次聚集闸）；写 `date_source`。
2. `ingest.py`/`facts_store.upsert`：valid_at 必须 `YYYY-MM-DD`、日历合法、≤ 今天+1，否则置空并记 doubt（拦 `2026-84-17` 这类）。
3. `_ingest_cross_market` 的 `source_kind` 别再硬编码 `social_research`，跟卡片走（顺手，非本题）。

### 4.3 `ingested_at` 要不要加
- 建议**加**，`ALTER TABLE facts ADD COLUMN ingested_at TEXT DEFAULT ''`（SQLite 元数据操作，秒级），`upsert` 首插时写 `datetime.now()`，合并路径不改；`backfill_background.py`/`dedup_same_claim.py` 首插同样写。**只用于回溯与校验 `valid_at ≤ ingested_at`，永不参与排序。** 存量留空即可（=不知道）。
- 也可以不加：这次问题不靠它解决；加了以后类似"这批什么时候入的"能答。属可选项，用户定。

### 4.4 顺序与窗口
1. 今晚：向量续建到 00:45 自动停；01:00 kbsync 正常跑（它今晚新入的 zsxq_research 卡仍会是空日期，属预期）。
2. 明天白天（任意时段，避开 01:00）：① 4.2 三处闸口改码 + 测试；② 存量修复脚本 `scripts/backfill_valid_at.py`（dry-run → `--apply`，带备份/锁/`--deadline`/`--undo`），跑一次覆盖存量 + 今晚新入的；③ 复跑 `valid_at_audit.py` 验收：空 valid_at 应从 32.4% 降到 ~2%、日历非法 0、未来 0。
3. 验收查询：`./tkb ask "东田微"`、`"晓程科技"` 看研报侧条目是否因时效项前移（现在 `_recency` 对空值给 0）。

## 5. 风险与反证
- **帖日期晚于发布 1~3 天**（94.5% 在 3 天内）：`_recency` 按 365 天线性衰减，3 天 = 0.8% 权重，可忽略。
- **老研报被重新转发**（5/7,249 = 0.07%）会被标成转发日：有文件名日期时文件名优先（现规则已如此），否则接受。
- **CreationDate 模板/归档日**：≥20 次聚集闸剔除；若用户不接受任何"打印日当发布日"，可把 L1 整体降为只在 L0/L2 都没有时启用（现已是）。
- **同名异日文件**（星球全库 1,115 个文件名重复）：同组匹配 + 极差 ≤3 天才填，干跑 AMBIG 为 0。
- 与 dc 数字的差异：dc 的"可恢复 8,885/86.5%"只算 ima；本方案加 L0 后 ima 达 9,368/91.2%，zsxq_research 15,125/99.2%。

## 6. 实施记录（2026-08-27）

- 00:53–00:56 `scripts/backfill_valid_at.py --apply`（用户 00:52「开跑」；kbsync 01:00 前 20 分钟为下载阶段不碰 facts.db，且每批 UPDATE 带乐观条件）：备份 `.backup/facts.db.bak_valid_at_20260827_005504`；**卡片写入 24,304 张**（留空 1,227：无来源 1,202 + 多源冲突 25），mtime 原样保留（抽检 3,000 张 `.ingested.txt` 键全一致，01:00 kbsync 不会重入）；**事实更新 560,336 行**（7 行因并发改动跳过），run_id `20260827_005504`，`--undo 20260827_005504` 可整体回滚。耗时 219s。
- 验收：active 1,842,742 中空 valid_at **597,024（32.4%）→ 37,986（2.1%）**；日历非法 133 → **0**、未来日期 3 → **0**。来源分布：zsxq_post 460,368 / pdf_creation 63,845 / filename 33,492 / pdf_mod 1,465 / cleaned_invalid 97。仍空的 37,986：broker_research 26,670 / foreign_ib 10,140 / social 1,176（无卡 5,096 + 无任何日期来源的卡）。
- `./tkb ask "东田微"`：研报侧（B 级 2026–2028 盈利预测、光隔离器/滤光片产品细节）已进入"可靠依据"段。
- 入库闸口三处已随 00:00 前的代码落地生效（`dates.clean_date` 于 `upsert` 入口、`ingested_at` 列自动迁移、`ima_to_card` 统一日期解析），222 tests 绿；kbsync 今晚新入的卡将自带 `date_source`。

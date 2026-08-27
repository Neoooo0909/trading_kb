# trading_kb 全面审核 · 发现与修复方案（2026-08-27）

> 状态：**方案待独立审核**。审核对象 = 分支 `recall-fix-20260825` 快照 `ed56df5`（多会话 08-25~08-27 未提交改动原样入库，已推 GitHub）+ `scripts/` 本地库 `3202f0c`。
> 方法：四路独立子 agent 逐行精读（数据层 / 摄入层 / 问答展示层 / 治理脚本与测试）+ 主审对每条 P0/P1 复核代码与只读查生产库。基线 `run_tests.py` 246 passed / 2 skipped。
> 四份原始报告在会话 scratchpad `audit_{A,B,C,D}_*.md`（不入库）。

---

## 0. 结论

1. **版本统一性**：只有一份代码（`~/trading_kb/src`），外部调用方（`~/ZSXQ/kb_adapter`、`scripts/`）都按绝对路径 import 它，不存在分叉副本；但**存在 4 类"手抄副本"漂移**：`_remap_doc_claim`×3 vs `FactsStore.doc_claim_remap`、`_ORDER_FAMILY` vs `ingest._ORDER_PROGRESSION`、kb_adapter 手抄 `ingest_card` 实体循环（漏 `is_pseudo_company` 闸门）、web JS 手抄 LEVEL_RANK×3。分支状态：`main` 落后 `recall-fix-20260825` 一个提交 + 本次快照，两者都已推远端。
2. **三轮修复方案的实现一致性**：RECALL（08-25）四项全部落地且无回归；BACKGROUND v3（08-26）主体落地，但 **LLM 分流钩子未跟**（`./tkb add/ingest` 路径 view 档被 LLM 打回 background）、`_structure_fallback` 口径漂移、多实体/例外行的判重登记漏洞；VALID_AT（08-26）落地，但回填脚本对 `valid_at IS NULL` 的 13 行永远失败并被误记为"并发跳过"。
3. **功能性 P0 三条**：① `critique`/`deep-check` 候选池按成色截断 → 生产库上恒为空（实测 top-5000 全是 A 级、0 条带质疑）；② LLM 分流钩子无 `view`；③ `clean_entities._reattribute` 碰撞分支纯 DELETE 丢 sources/成色，6 个夜跑脚本共用、无归档。
4. **历史问题复发 5 条**：#9 splitlines 读 JSONL（4 个夜跑脚本）、#4 裸 cp 备份（ipo/rebuild_clean）+ 8 处裸连接无 busy_timeout、#6 退出码恒 0（cli.main / run_daily_extract / ingest_kb_cards）、#12 告警在零结果场景被渲染层吞掉、#23/#12 伪公司经 kb_adapter 路径再生（08-27 已新增 91 个）。
5. 生产库当前三层索引零漂移（fts_map = vectors = active 1,869,825；doc_claim 悬空 0；relations 悬空 0），说明日常对账机制在工作；本方案不需要动生产数据，只需在部署后跑一次 `docclaim build`（含迁移）与一次 `critique` 建索引。

---

## 1. 发现清单（编号供后文引用）

### P0

| # | 位置 | 缺陷 | 后果 |
|---|---|---|---|
| F1 | `cli.py:101,119` `web.py:106` | critique/deep-check 候选池 `facts.query(limit=5000)` 默认成色降序截断；生产库 A 级 15.5 万条，前 5000 全是 A 级 hard_fact 且 A 级带质疑数 = 0（B/B+/C/D 带质疑 34 万条） | 质疑榜、web 质疑 tab、deep-check 在当前库上恒为空，不报错；且该查询 21s |
| F2 | `llm.py:20,72-79` `classify.py:249-252` | `_CATEGORIES`/分类 prompt 无 `view`，prompt 把"观点"定义成 background；`classify_finding` 无条件采纳 LLM 覆盖 | `./tkb` 启动器默认 `TKB_USE_LLM=1` → `./tkb add/ingest`/web 重摄入路径 v3 的 view 档整体失效（落 background_log、reason 还标 `no_entity_no_number`）；两 lane 分流口径不一致 |
| F3 | `scripts/clean_entities.py:74-76` | `_reattribute` 目标 fact_id 已存在时纯 `DELETE` 旧行，不并 sources/support/成色/entities/doubts、无归档 | merge_fragments/concept/english/typo、llm_attribute_unknown、retype 共用，每晚 `--apply`；多源印证与成色静默丢失，不可回滚；`test_reattribute_merges_when_target_exists` 只断言行数=1，把错误行为钉住 |

### P1

| # | 位置 | 缺陷 |
|---|---|---|
| F4 | `facts_store.py` doc_claim 主键 `(doc_id, ckey)` + `INSERT OR IGNORE` | 多证券"例外"放行的第二行永远登记不上（生产 174 行不受判重保护），口径漂移再入库照样造第 3 行；悬空行占位后新登记被 IGNORE，`docclaim build` 修不了（cli 提示"跑 build 后再查"不成立） |
| F5 | `facts_store.py:552` 合并路径 | 只登记 `existing.claim`；`dedup_key` 的 object=`claim[:80]`，80 字后不同的 claim 变体永不登记 → 换 cid 再入库绕过判重（生产 claim>80 字 10.1%） |
| F6 | `semantic.py:285-289` | `_save_matrix_cache` 快路径 `del self._mat` 后 `os.replace` 失败 → `_mat` 属性消失，进程内语义检索永久 `[]`（web 常驻要重启） |
| F7 | `~/ZSXQ/kb_adapter/ingest_kb_cards.py:215-224`、`backfill_background.py:215`、`_ingest_cross_market` | 手抄实体登记循环漏 `is_pseudo_company` 闸门；生产 entities 08-27 11:27 起新增 91 个伪公司（83 个挂事实），retype 两天后再生 |
| F8 | `ingest.py:235-243 _structure_fallback` | 判"有实体"用任意非空串而非 `_has_real_entity` → 仅垃圾端的 structure 句变成 `subject=未知主体` 的 view（生产 171 行）；`structure_garbage_end` 留痕恒 0 |
| F9 | `deep_verify.py:162-222 auto_verify_fresh` | 不过滤 `category=="view"`，绕过 `deep_verify_fact` 的 hard_fact 闸；view 恒 C/D，生产 45,576 条符合条件，`TKB_USE_WEB=1` 时 max_n=5 名额被 view 占满，且可被澄清公告判 contradicted → `mark_disputed` 写库（当前 disputed view=0，未发生） |
| F10 | `ask.py:35-38` + `web.py:434-437` | 无结果早退不渲染 warnings → "锚到低覆盖实体已切发现模式/字形纠错/FTS 降级"在零结果场景两端都被吞（历史 #12） |
| F11 | `ask.py:346,539-545` + `to_payload._extra` | `_fact_extra` 对 `extra='[1,2]'`（合法 JSON 非对象）/`entities` 含 null 崩 → 一行坏数据让整次 ask 崩、web 500；五处各写一套 extra 解析 |
| F12 | `cli.py main()` 恒 `return 0`；`add/semantic/feed-chat/deep-check/critique` 失败路径 `print ✗; return` | 编排层永远 rc=0（§2.2） |
| F13 | `scripts/announcements_to_kb.py:84-86` | `_ORDER_FAMILY` 仍手抄，ARCHITECTURE §2.3 写"已收敛"与代码不符 |
| F14 | `scripts/ipo_prospectus/ingest_to_kb.py:116`、`~/ZSXQ/kb_adapter/rebuild_clean.py:50-57` | `shutil.copy` 裸拷 WAL 库做备份（§2.4 明禁）；rebuild_clean raw DELETE 后不清 FTS/doc_claim/vectors |
| F15 | `scripts/backfill_valid_at.py:200-248` | `old = valid_at or ""` 但乐观 UPDATE 用 `AND valid_at=''`，NULL 永不匹配 → 13 行 NULL 中 7 行有日期建议永远修不了，方案文档"7 行因并发改动跳过"归因错 |
| F16 | `llm_attribute_unknown.py:111,172,229,310`、`merge_concept_companies.py:81,120`、`merge_english_fragments.py:100,143,181,187`、`merge_typo_fragments.py:69,107,115,120,158` | `read_text().splitlines()` 读含 LLM 自由文本的 JSONL（历史 #9 复发）；5 处无 except → 一个 U+2028 让 apply 阶段崩且 rc 无人看 |
| F17 | `clean_entities.py:129`、`merge_*.py`×4、`llm_attribute_unknown.py:131,167,318`、`fix_relation_merged_refs.py:29`、`ir_qa_to_kb.py:39` | 裸 `sqlite3.connect` 无 `busy_timeout=30000`（§2.4），夜跑与 web/回填并发即 `database is locked` 崩 |
| F18 | `~/ZSXQ/kb_adapter/run_daily_extract.sh:235-335`、`ingest_kb_cards.py main()` | 入库/D 段治理/E 段语义/F 段 FTS 不接 rc，ingest 恒退 0，只有 A 段失败上抛（§2.2） |
| F19 | `clean_entities.py:83`、`requalify_quant.py:97`、`dedup_same_claim.py:238` vs `FactsStore.doc_claim_remap` | `_remap_doc_claim` 三份手抄，`doc_claim_remap` docstring 声称被它们调用实为零调用（§2.3） |
| F20 | `web_enrich.py:84-91`（latent，USE_WEB 默认关） | 验证主体取 `entities[0]`，与 `_pick_subject`（跳垃圾/投行）不一致 → 查错主体的公告判 `no_evidence` 降档 |
| F21 | `facts_store.py:466-472` dup 分支 `except Exception: pass` | 无声吞错（§2.2） |
| F22 | `ask.py:519-536 _low_grade_views` 无上限、前置证据链 | 回填后单次 ask 情绪面 95–137 条（≈9.6k 字，占 24k 窗口 40%），debate/deep_ask 的 5–7k 窗口被整体挤占——**与用户 07-21 "不设条数上限"的决定冲突，见 §3 决策点 ④** |

### P2（本轮一并修的，见 §2 各条括注；未修的列在 §4）

---

## 2. 修复方案（按模块；每条给出改法、测试、回滚）

### 2.1 数据层 `facts_store.py`

**F4+F5+F21 · doc_claim 判重收口**
- 主键改 `(doc_id, ckey, fact_id)`（仍 WITHOUT ROWID，另保留 `idx_doc_claim_fid`）。新库直接建新表；**旧库迁移放在 `doc_claim_build()`**（检测 `PRAGMA table_info` 的 pk 列数 <3 → `BEGIN IMMEDIATE` 建新表 INSERT SELECT 换名，1.98M 行预计 <1 min），不在每次 `FactsStore()` 打开时做——避免多进程同时迁移抢锁。迁移前旧主键下行为不变（例外行仍登记不上），迁移后自愈。
- `doc_claim_find` 改返回**全部**同 (doc, ckey) 且 facts 中存在的行（JOIN 天然忽略悬空）；upsert 判重：任一行满足"非双证券异码"→ 判重命中（优先取 active 行返回）；全部行都是"双证券且与新 fact 异码"→ 放行并登记（新主键下登记成功）。
- 合并路径同时登记 `existing.claim` 与 `fact.claim`（ckey 不同时各占一行）。
- `doc_claim_build` 开头 `DELETE FROM doc_claim WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.fact_id=doc_claim.fact_id)`；`doc_claim_status` 的悬空提示改为"跑 build 清理"。
- dup 分支 `except Exception` → `_warn_once`；dup 分支多 source 场景：把未命中的 source 并入 sources（P2-3）。
- INSERT 后非 `OperationalError` 异常 → `rollback` 后 `raise`（P2-4，防半成品被下一张卡 commit）。
- `search`：`_fts_terms` 与 LIKE toks 都为空 → 直接返回 `[]`（P2-6，治单字查询灌 400 条无关行）。
- `claim_key` 注释改实（与 backfill `_ckey` 不同口径，互不交换）。
- 新增 `query_with_doubts(limit, order)`（供 F1）：`WHERE status IN(...) AND json_extract(extra,'$.doubt_severity') IS NOT NULL`，按 severity 排；配套**部分表达式索引** `idx_facts_doubt ON facts(json_extract(extra,'$.doubt_severity')) WHERE json_extract(extra,'$.doubt_severity') IS NOT NULL`，在 `_init_schema` 用 `CREATE INDEX IF NOT EXISTS` 建（首建全扫一次，部署后由我单进程先跑一次 `./tkb critique` 承担）。
- 新增 `stats()` 字段：by_category、background_log、doc_claim rows/dangling、fts 覆盖（供 `tkb stats`，历史 #27）。
- 新增静态方法 `doc_claim_remap_conn(conn, old, new)`，实例方法转调它；三份手抄（F19）改调它。
- 测试：`test_doc_claim.py` 补"双证券第二行能登记且再入库被拦""长 claim 变体合并后两 ckey 都登记""悬空行不遮蔽新登记且 build 清掉""旧主键库 build 自动迁移"；`test_facts_store` 补"空 term search 返回空""INSERT 后异常回滚"；`query_with_doubts` 在 5000 条 A 级无质疑 + 1 条 C 级有质疑的库上必须返回那 1 条。

**F6 + semantic P2 · 矩阵缓存**
- `_save_matrix_cache` 快路径：不 `del`，`old=self._mat; self._mat=None` → replace 失败时 `self._mat=old`（继续用 .tmp memmap，出声）；`_load_matrix` 首行 `getattr(self,"_mat",None)`。
- `_load_matrix` 用 try/finally 清 `.tmp/.tmp2`（异常路径）；快路径条件改 `from_tmp is not None and rows == shape[0]`（允许 0 行）。
- `_cache_key` 在**装载开始前**取值并写入 ids.json（P2-25 竞态）。
- 测试：monkeypatch `os.replace` 抛 OSError → `search` 仍返回结果且 `_mat` 存在。

**entity_registry P2**
- `merge`：`from_id == into_id` 或 `_follow_merge(into_id) == from_id` → 出声并 return（不造环/自指）。
- `_sync_relations`：rel_id 改用 `Relation(src,rel_type,dst).rel_id`（去手抄）；并入时重算 `low_confidence=int(len(srcs)<2)`；`except (sqlite3.Error, ValueError)`；告警打 stderr。
- **伪公司闸门下沉到 `register`（F7 治本）**：`type_=="company" and not stock_code and is_pseudo_company(name)` → 以 `concept` 登记。`ingest_card` 的同款判断保留（幂等）。测试：经 `resolve("北美AI集群", type_="company")` 得到 `concept:` 前缀。
- 测试补 merge 自合并/成环、并入后 low_confidence。

### 2.2 摄入层

**F2 · LLM 分流钩子**（`llm.py` + `classify.py`）
- `_CATEGORIES` 加 `view`；prompt 加一行 `view: 有明确主体的定性判断/观点(无可证伪数字或事件)`，background 定义改为"无主体的宏观/情绪/套话"。
- `classify_finding` 的 override 只在规则结果 ∈ {view, background}（低置信档）时采纳；且 `override=="background"` 而 `_has_real_entity(f)` 为真时不采纳（保 view）。注释改实（原注释即此意）。
- 测试：mock LLM 答 background → 规则 view 保持 view；mock 答 hard_fact → 规则 view 升 hard_fact；规则 hard_fact 时 LLM 答 background 不降。

**F8 · `_structure_fallback`** 用 `classify._has_real_entity(f)`（并把它导出为公共名 `has_real_entity`），否则 `log_background(reason=...)`；`_ingest_structure` 里 `resolve` 结果 == `UNKNOWN_CID` 的端不建边（D-P2-13，生产 464 条"未知主体"hub 边不再新增）。测试：仅垃圾端 structure → `background_log(structure_garbage_end)`。

**留痕 reason 分档**（B-P2）：`classify_finding` 增返回原因的姊妹函数 `classify_with_reason(f, llm)`（原函数保留签名），ingest 用它写 `reason ∈ {boilerplate, llm_override, no_entity_no_number, structure_*}`。

**dup 时不累加 doubts**（B-P2）：`_ingest_fact` 在体检前先 `facts.doc_claim_find`，命中且非例外则跳过体检，直接 upsert（upsert 再判一次是幂等的）。

**F20 · `web_enrich._fetch_recent`** 选实体改为"首个非垃圾、非投行"的实体（与 `_pick_subject` 同口径的最小子集，不引入卡片依赖）。

**`_DATEONLY_NUM_RE`**（B-P2）：年份分支收窄 `20[0-3]\d`；补 `20\d{2}年\d{1,2}月`、`Q[1-4]|H[12]` 组合（`26Q1/25H2`）、`[上下]半年|年[初中底]`、`\d+[-~至到]\d+\s*(年|月)`、英文 `\d+\s*(months?|weeks?|years?)`、`[A-Z][a-z]+\s*20\d{2}`；参数化测试 20 例（含误拦对照 `5000`、`1330` 必须算真数值）。

**grade.py**：`social_research` 显式登记基线 C（B-P2）。

**回执**：`IngestReport.dup_skipped` 打进 cli/web/kb_adapter 回执；web 前端重摄入卡片显示 views/background/dup。

### 2.3 问答/展示层

- **F1**：`cmd_critique`/`cmd_deep_check`/`web.critique_payload` 改用 `facts.query_with_doubts(limit=5000)`，回执打印"候选池 N 条（只取带质疑行）"。
- **F9**：`auto_verify_fresh` 跳过 `category=="view"` / `predicate=="HAS_VIEW"`；`cmd_ask` 只对 hard_fact/quant_fact 回写 disputed。测试：view 行不进 elig。
- **F10**：`to_six_section` 早退分支也渲染 `## ⚠ 检索告警`；web `renderAsk` not-found 分支渲染 `d.warnings`。
- **F11**：`_fact_extra` 非 dict → `{}`；entities 过滤非 str；`to_payload._extra`/`_vn`/`_doubts`/`_doubt_icon` 统一走 `_fact_extra`。测试：`extra='[1,2]'`、`entities=[null,"x"]` 不崩。
- **F12**：各 `cmd_*` 返回 int（失败 2），`main` 透传。测试：`feed-chat` 文件不存在 → rc 2。
- **F22（决策点 ④）**：默认**不加上限**（遵从用户 07-21 决定）；加 `config.SENTIMENT_MAX_VIEWS`（环境变量 `TKB_SENTIMENT_MAX_VIEWS`，默认 0=不限）；超限时段末打印"另有 N 条低成色观点未列"。debate/deep_ask 的硬编码窗口收到 `config.SYNTH_MATERIAL_CHARS` 比例（`DEBATE_MATERIAL_CHARS`）。
- payload/文本对 view 打标：`to_payload` 的 conclusion/evidence 项带 `category`；结论段全 view 回退时标"(定性论断·无硬事实)"；证据链行 view 标 `[观点]`；web 据 category 渲染标签。
- web LV/LVCLS/lvbar 三处手抄改由 `PAGE` 注入 `json.dumps(list(LEVEL_RANK))`；`ingest.level_dist` 初始化从 LEVEL_RANK 生成。
- `tkb stats` 打印 by_category / background_log / doc_claim / fts 覆盖；`cmd_docclaim` 补 `ensure_data_dir()`。
- 语义层不可用（`_semantic_recall` 拿到 None）→ `warnings` 追加"语义层不可用,仅关键词召回"；FTS 降级 → `FactsStore.last_search_mode` 暴露给 ask，`warnings` 注明"关键词召回已降级为 LIKE"。
- `--audit` 历史行：`query` 加 `statuses=` 参数直取 superseded/invalidated/expired 行（limit 120），并打印"另有 N 条"。
- followup 截断统一 80。`revalue.reweight_note` 快慢变量统计跳过 view。
- `tkb` 启动器：`mkdir -p` 副本目录、路径改 `${TKB_DATA_DIR:-$DIR/data}`、`python3 -u`。
- ARCHITECTURE §1 措辞改"web 不得 import cli（cli→web 懒加载允许）"。

### 2.4 治理脚本（`scripts/`，独立 git 库）

- **F3**：`_reattribute` 碰撞分支改真合并：读目标行 → sources ∪ / support=len / level 取高（`models.LEVEL_RANK`）/ valid_at 取非空最早 / unverifiable AND / extra.entities ∪ / doubts ∪ → UPDATE 目标 → 旧行整行 JSON 写 `facts_merged_archive`（表不存在则按 dedup_same_claim 的 DDL 建）→ DELETE 旧 → `doc_claim_remap_conn` → 清旧 id 的 fts_map/facts_fts 行（表不存在静默）。测试改为断言 sources==['a','b']、support=2、level 取高。
- **F13**：`from trading_kb.ingest import _ORDER_PROGRESSION`；`_ORDER_FAMILY = tuple(_ORDER_PROGRESSION)`；governance 测试断言相等。
- **F14**：ipo `ingest_to_kb` 与 kb_adapter `rebuild_clean` 改 `_db_backup.backup_db`；rebuild_clean DELETE 后调用 `tkb fts build` + `docclaim build` + `semantic build`。
- **F15**：`apply_facts`/`undo` 乐观条件改 `COALESCE(valid_at,'')=?`；测试补 `valid_at=None` 行。
- **F16**：新增 `scripts/_jsonl.py::iter_jsonl(path)`（按文件行迭代，坏行出声计数不吞），四脚本全部替换 `splitlines`；`backfill_valid_at` 两处一并。
- **F17**：新增 `scripts/_db_backup.py::open_db(path, ro=False)`（WAL 库统一 `busy_timeout=30000` + `Row`），8 处裸连接替换；`fix_relation_merged_refs` 分批 commit。
- **F19**：三处 `_remap_doc_claim` 改调 `FactsStore.doc_claim_remap_conn`。
- `_db_backup.backup_db` 失败删半截 dst；`prune_backups._GOVERNANCE_PROCS` 补 6 个新脚本名；补 `_plan_big`/`_RE_BIG` 测试（成组同去同留、30 天闸、日更/ipo 不误吞）。
- `backfill_background`：命中判重时计 `existing_skipped` 而非 `new_*`；补"`_fact_id_for` 与 `_ingest_fact` 派生恒等"测试。

### 2.5 外部调用方（`~/ZSXQ/kb_adapter`，同批改，需用户知悉：不在 trading_kb 仓库内）

- **F7**：闸门下沉后 `ingest_kb_cards.py` 无需改逻辑；回执加 `dup_skipped`；`_ingest_cross_market` 的 `source_kind` 改跟卡片（VALID_AT §4.2.3 顺手项）。
- **F18**：`run_daily_extract.sh` 每步 `rc=$?` 记录，任一非零 → 末尾 `exit 3`（与 A 段同口径）；`ingest_kb_cards.main` 在"有待入库卡且全部脏"或未捕获异常时 `sys.exit(2)`；F 段之后挂 `./tkb docclaim build`（D-P2-17）。
- `sync_all.sh` 过时注释（journal_mode=delete）改 WAL。

### 2.6 文档

- ARCHITECTURE：§2.3 表补 `doc_claim_remap_conn`、`_ORDER_FAMILY` 已收敛、伪公司闸门在 `EntityRegistry.register`；§3 补 `facts_merged_archive`/`valid_at_backfill_log`/`idx_facts_doubt`、doc_claim 主键；§3 约束段删"merge 不回写 relations"（已回写）；§4 夜跑段按 run_daily_extract 实况改；§5 加"debate/deep_ask 窗口"。
- BACKGROUND_FIX_PLAN §6.2 合并路径登记表述改实；VALID_AT_FIX_PLAN §6 "7 行并发跳过"改为"NULL 盲区，本轮已修，部署后重跑 `backfill_valid_at --apply` 补 7 行"。

---

## 3. 需要拍板的决策点（主审推荐已标注，独立审核请逐条挑战）

| # | 决策 | 推荐 | 备选 |
|---|---|---|---|
| ① | F1 带质疑行的取数 | 部分表达式索引 `json_extract(extra,'$.doubt_severity')`（首建全扫一次 ~30s，之后毫秒） | 无索引全扫（每次 critique ~15–25s，可接受但慢） |
| ② | F2 LLM 覆盖策略 | 仅低置信档（view/background）可被覆盖，且不允许"有主体却打成 background" | 保持无条件覆盖仅补 view 词（LLM 仍会把部分 view 打回 background） |
| ③ | F4 doc_claim 主键迁移 | 迁移放 `docclaim build` 显式执行（部署后我单进程跑一次，写锁 <1 min，避开 01:00）；打开库不迁移 | 不改主键，例外行用 `ckey⊕hash(cid)` 命名空间（无迁移但语义晦涩） |
| ④ | F22 情绪面条数 | **不设默认上限**（用户 07-21 决定优先），只加可选环境变量 + 超限提示 | 默认上限 40 |
| ⑤ | 是否同批修改 `~/ZSXQ/kb_adapter`（仓库外） | 是（F7 回执/F14/F18 均为管线正确性），改前备份到 `.backup/` | 只改 trading_kb，kb_adapter 留待用户 |
| ⑥ | `scripts/` 库无 GitHub 远端（.gitignore 注明"不对外暴露"） | 本轮只本地提交；是否建**私有**仓由用户决定 | 建私有仓 `trading_kb_scripts` 并推送 |

## 4. 本轮明确不做（登记，防范围失控）

- doc_claim 存量 59 条"同文同句同 cid 异谓词"重复（需人工看样例定合并规则）。
- `dedup_same_claim` 全表常驻内存改流式；`retype_pseudo_companies` 三库原子性。
- 快路径 `_locate_entity` 全量别名匹配、`_search_fts` 带 cid 先全库后过滤（当前 ask 不传 cid）。
- `structure_store`/`facts_store` 乐观重试抽基类（ARCHITECTURE §5.3）。
- `test_scripts_governance` 对 `~/report_lab` 的导入依赖。
- `prune_orphan_relations` TOCTOU/无锁（当前孤儿 0）。
- `.backup/structure.db.bak.20260629_201632` 手工清理。

## 5. 实施顺序与验收

1. 数据层（2.1）→ 摄入层（2.2）→ 问答层（2.3）→ 脚本（2.4）→ kb_adapter（2.5）→ 文档（2.6），每层改完跑 `run_tests.py`。
2. 全绿后：独立交叉验证 agent 按本文 §1 逐条核对"缺陷是否消除、测试是否钉住、无新回归"。
3. 部署动作（生产库，避开 01:00）：`./tkb docclaim build`（迁移+清悬空）→ `./tkb critique`（首建 idx_facts_doubt，验证非空）→ `./tkb stats` → `python3 scripts/backfill_valid_at.py --apply`（补 7 行 NULL）。
4. 合并 `recall-fix-20260825` → `main`，推 GitHub；`scripts/` 本地提交（决策 ⑥）。

## 6. 回滚

- 代码：分支快照 `ed56df5`（GitHub）/ scripts `3202f0c`；每个被改文件另存 `.backup/<name>.<ts>`。
- 生产库：本方案不改事实数据；`docclaim build` 迁移前 `_db_backup.backup_db` 热备 facts.db（BIG 命名 `facts.db.bak.docclaim_<ts>`）；`idx_facts_doubt` 可 `DROP INDEX`。

---

## 7. 独立审核结论与采纳（2026-08-27，对抗性审核：有条件通过）

审核报告全文在会话 scratchpad `plan_review.md`（3 组临时库实验 + 生产只读查数）。六项条件**全部采纳**，主要修正：

| 审核条件/建议 | 采纳 | 对方案的修正 |
|---|---|---|
| ① doc_claim 主键改三元后，现有 `UPDATE doc_claim SET fact_id=…` 会撞唯一约束（实验 E1：keeper 与 loser 同 (doc,ckey) 时 IntegrityError），迁移当晚 dedup/merge_* 必崩 | 采纳 | 唯一 remap 实现 `FactsStore.doc_claim_remap_conn` 用 `UPDATE OR REPLACE`；dedup/_reattribute/requalify 全部改调它；补测试"同 (doc,ckey) 双登记 remap 不抛、只剩 keeper" |
| ② `idx_facts_doubt` 不得在 `_init_schema` 建（首建 ≥ busy_timeout 会让并发进程在构造函数里死；一行非法 JSON 让索引永远建不起来） | 采纳 | 新增 `./tkb migrate`：json_valid 前置 → 建部分索引 → doc_claim 主键迁移 → 清悬空 → `PRAGMA user_version`；`docclaim build` 先调 migrate。`query_with_doubts` 缺索引时降级 LIKE 全扫（~15s）并出声；ORDER BY 用 CASE 映射 severity，`+status` 禁索引 |
| ③ F2 缺对称闸门：LLM 把无主体的 background 改成 view 会造"未知主体 view"（F8 同款） | 采纳 | override→view 须 `_has_real_entity`；override→background 须无真实实体；override→quant_fact 须 `_has_metric`；LLM 实际只保留"把规则漏掉的硬/结构/量化捞回"的能力 |
| ④ F3 不得再造第四份合并口径 | 采纳 | `dedup_same_claim.merge_rows` 上提为 `facts_store.merge_fact_rows`（纯函数）+ `ensure_merged_archive`/`archive_fact_row`；dedup / requalify `_merge_into` / `_reattribute` 三处共用；目标行非 active 时不合并、出声跳过（同 dedup） |
| ⑤ 闸门下沉后 `test_recall_fix.py:26,184` 的存量伪实体模拟失效；已再生的 91 个不会被回收 | 采纳 | 测试改 raw INSERT 造存量行；部署步骤补 `retype_pseudo_companies.py --apply` 重跑 + `fts build` |
| ⑥ 拆两批 | 采纳 | 第一批：F3/F19/F4/F5/F21/F7/F15–F18/F14/F2/F8/F9/F1/F6/F11/F12（+`supersede` nid 护栏、`clean_date` 接受 `YYYY/MM/DD` 与 8 位数字）；第二批：F10/F13/F22/view 打标/LEVEL_RANK 注入/stats/warnings/`--audit`/followup/revalue/启动器/`_DATEONLY_NUM_RE`(带 2000 卡回放)/grade/回执/文档。各批独立提交、独立跑全套测试 |
| F12 退出码要区分"真没数据"(rc 0)与故障(rc 2) | 采纳 | critique/deep-check 空池 rc 0 + 一行说明；rc 2 只给异常/依赖缺失；连带效应：kb_sync ③ 段会因 semantic/fts 失败整段重跑一次（符合 §2.2 本意，写入 ARCHITECTURE §4） |
| F18 脏卡"过半"退 2；`pgrep -f` 误判修为 mkdir 原子锁 | 采纳 | — |
| F22 补"LLM 材料预算"备选 | 采纳为**默认不改变现状**的折中 | 展示层不设上限；LLM 材料里情绪面预算 `SENTIMENT_MATERIAL_SHARE`=0.5×窗口（12k 字≈170 条，当前 95–137 条不会触发），超出以"另有 N 条"代替；debate/deep_ask 窗口收进 config。**用户可改**：置 0 = 不限 |
| `iter_jsonl` 放 `src/trading_kb/jsonl.py` 供 scripts 与 kb_adapter 共用 | 采纳 | — |
| "后续验证"段跳过 view；`FactsStore.extra_of(row)` 统一 extra 解析 | 采纳 | — |
| P2-3 dup 分支多 source 并入 | **本轮不做**（当前调用方全单 source，规格待定） | 登记 §4 |
| F6 回退后 `.tmp` 被下个进程覆写 | 采纳 | 回退时若内存可容纳则 `np.array` 拷入内存，否则出声"请重启" |
| 新决策点 ⑦ LLM 分流默认开启（收窄为只升硬后每条 finding 一次调用） | 不改默认，登记给用户 | `./tkb` 启动器 `TKB_USE_LLM=1` 是用户既定口径；量化"升硬命中率"作后续任务 |

---

## 8. 实施记录（2026-08-27）

- **第一批**（提交 `2e83120` / scripts `4ff0a5e`，14:25）：F1–F9、F11、F12、F14–F21 + `supersede` 护栏 + `iter_lines`。
  `tests/test_audit_fix_20260827.py` 30 例；全套 276 passed / 2 skipped。`test_recall_fix.py` 两例改 raw 行模拟存量伪公司（审核条件 ⑤）。
- **部署（生产库，14:25–14:33，kbsync/回填均未运行，lsof 无写者）**：
  - 热备 `.backup/facts.db.bak.audit_20260827_142533`（backup API，2.9GB，8s）
  - `./tkb migrate`：schema v0→v2，doc_claim 主键迁移 + `idx_facts_doubt` 建成，36s；`./tkb docclaim build` 新登记 632 条（此前登记不上的例外行等）、悬空 0，35s
  - `./tkb critique` 从恒空变为有料：库内带质疑 341,082 条；EXPLAIN 走 `idx_facts_doubt`；分档等值点查后耗时 8.7s（其中 count 与 5000 行回表为主）
  - `retype_pseudo_companies.py --apply`：改型 91 个 08-27 再生的伪公司，重挂事实 92 / 关系 84，三库热备 `*.bak.retype_20260827_142825`；`fts build` 对账 0 漂移；doc_claim 悬空 0
  - `backfill_valid_at.py --apply`：卡片层无新可补（1,227 仍无来源）；事实层 13 行 NULL 由定向脚本按卡片已回填日期走 `apply_facts`（run_id `20260827_null_fix`，可 `--undo`）补齐
- **第二批**（本提交）：F10 补齐、F13、F22（展示不限 + LLM 材料预算 `SENTIMENT_MATERIAL_SHARE`）、view 打标/payload category、web `LEVEL_RANK` 注入、stats 扩展、语义/FTS 降级进 warnings、`--audit` 直取历史行、debate/deep_ask 窗口收进 config、revalue 跳 view、grade 显式登记 social_research、`tkb` 启动器、`_DATEONLY_NUM_RE` 收窄/扩展、文档同步。
  全套 284 passed / 2 skipped。
- **`_DATEONLY_NUM_RE` 生产回放**（4 个 lane 各 500 卡只读回放，新旧规则类别迁移）：cards 1.26%（36 hard→view / 2 view→hard）、ima 0.46%（36/9）、zsxq_research 0.40%（52/3）、report_lab 0.21%（7/3）。
  hard→view 的都是只含时长/日期区间的句子（"库存仅能维持3至6个月""认证周期1~2年""7月16日为申购日"），view→hard 的是 `5000/1330/1800/3810` 这类被旧式当年份误拦的真量值——方向均符合预期。存量不重判（fact_id 不变原则），只影响新入库。
- **未做/待用户拍板**：§4 清单；决策 ⑥ scripts 库仍无远端（本地提交）；决策 ⑦ LLM 分流默认开启的量化。

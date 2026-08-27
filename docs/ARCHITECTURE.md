# trading_kb 架构文档（v0.5 目标架构）

> 2026-08-16 全面审查后确立。本文档是架构的**唯一权威描述**：分层规则、跨模块协议、
> 单一定义点清单、并发与备份策略。改代码前先对照本文；违背本文的实现视为缺陷。
> 变更前完整备份 `.backup/pre_overhaul_20260816/` **已于 2026-08-26 清理**（占 5.4G，其中 4.2G 是
> 可由 `tkb semantic build` 重建的向量库；facts/entities 停在 8-16，其后经历补跑 3.5 万条事实、
> FTS5 建表、伪实体改型、relations 修复，已不构成可用回滚点）。当前回滚点见 §备份策略：
> `.backup/` 日更成对备份（`com.tkbprune.daily` 每天 12:00 轮转，保留 3 天 + 每月首份）。

---

## 1. 分层与依赖规则

```
应用层   cli.py          web.py            ← 只做参数解析/渲染/HTTP，不含业务规则
           │                │
服务层   ask.py  ingest.py  revalue.py  deep_ask.py  debate.py  deep_verify.py
         announcement.py  web_enrich.py  verify_hooks.py  llm.py  report_lab_adapter.py
           │                │
领域层   classify.py  grade.py  critique.py  entity_quality.py     ← 纯函数，零 I/O
           │                │
数据层   facts_store.py  structure_store.py  entity_registry.py
         sentiment_lane.py  semantic.py  hypothesis.py
           │
核心层   models.py  config.py                                      ← 零内部依赖
```

**规则**：
1. 依赖只向下，同层之间尽量不互相依赖。**cli 与 web 之间禁止任何方向的 import**
   （历史上 web→cli 借 `_TS_RE` 成环，已把碎片解析下沉到 `sentiment_lane.parse_fragments`）。
2. 领域层保持纯函数、可离线复现；一切 I/O（网络/LLM/库）通过**构造注入的钩子**进入
   （ingest 的 verify/llm_classify/critique_engine，deep_ask/debate 的 complete）。
3. 数据层的 `conn` 是私有实现。应用/服务层需要的查询一律加公共方法
   （如 `FactsStore.count_active()`、`SemanticIndex.vector_count()`），不得裸摸 conn/_conn。
4. 外部项目（report_lab 的 LLM 链、~/tdx、~/ifind_ft）通过 `llm.py`/`revalue.py` 的
   适配函数接入；加载方式用 importlib 按绝对路径，或 sys.path **append**（不抢 [0] 位）。

---

## 2. 跨模块协议（本次审查确立的硬约定）

### 2.1 verify 三态协议（成色升降级的语义基石）

`grade_fact(finding, verify=...)` 的 verify 钩子返回值**只有四种合法语义**：

| 返回值 | 语义 | grade 的处理 |
|---|---|---|
| `"confirmed"` | 权威源查到**与该事实同主题**的证据 | 升 A，清 unverifiable |
| `"refuted"` | 权威源明确打脸 | 降 D（反证走 CONTRADICTS） |
| `"no_evidence"` | **真的查了**，权威源没有对应证据 | 降一档（不低于 C），保留 unverifiable |
| `None` | **没查**（未接通/异常/该谓词无验证动作） | 保持基线，保留 unverifiable |

历史缺陷（v0.4 及以前）：`None` 被当"查无"降级 → 空桩 verify_hooks 导致
"从未实查却全体降级"；同时 web_enrich 用"公司存在任意公告"当 confirmed →
"研报订单说法被任意公告洗白成 A"。两者互为镜像，根因都是三态未定义。**新代码
一律遵守本表；写新的 verifier 时禁止把"没查"与"查无"折叠。**

`web_enrich._verify_via_authoritative` 的 confirmed 判据：公告标题须命中
**谓词对应的主题关键词**（订单→中标/合同/订单…，产能→扩产/投产…，交付→交付/验收…），
"该公司存在任意公告"永远不构成 confirmed。

### 2.2 失败必须出声（运维脚本的退出码协议）

- launchd/编排入口的 wrapper（run_announcements.sh / run_ir_qa.sh）必须捕获每步 rc
  并以非零退出，让 `sync_all.sh run_step` 的重试与告警生效。
- 取数脚本必须能区分"真没数据"与"链路故障"：工作日全零（抓取 0 + 降级 0）→ exit 2；
  分类别抓取异常要计数并在收尾打印，异常类别过半 → exit 2。
- `except Exception` 允许用于降级，但降级点必须向 stderr 打一行告警（不许无声）。
- 日更管线必须自愈：启动时检查库内最近入库日期，自动回补缺口（上限 14 天），
  参照 ipo_prospectus 的"滚动窗 + 已做即跳"模式。

### 2.3 单一定义点清单（改这里，别处自动跟随）

| 语义 | 唯一定义点 | 曾经的重复处（已收敛） |
|---|---|---|
| 成色排序权重 | `models.LEVEL_RANK` | facts_store._LEVEL_RANK / query SQL CASE / hypothesis._LEVEL_W / web 前端 LV |
| ask 结构化结果 | `AskResult.to_payload()` | web.ask_payload 手工拼装（曾带已修复的 [:8]/[:12] 截断旧 bug） |
| 质疑榜排序 | `critique.collect_doubts()` | cli.cmd_critique 与 web.critique_payload 各一份 |
| 聊天碎片解析 | `sentiment_lane.parse_fragments()` | cli._read_fragments 与 web.feed_payload 各一份（含 _TS_RE） |
| 订单事实状态机 | `ingest` 的 _ORDER_PROGRESSION（公告 lane 引用它，不再手抄键集） | announcements_to_kb._ORDER_FAMILY |
| Fact 主键口径 | `models.Fact.fact_id`（sha1(dedup_key)[:16]） | scripts/clean_entities._reattribute 手抄（已加不变量测试钉住） |

### 2.4 SQLite 并发与备份策略

- 所有库连接统一：`journal_mode=WAL` + `busy_timeout=30000`。WAL 使读写不互斥，
  与真实并发形态（每日 cron + web 常驻 + 手动 CLI）匹配。
- **WAL 之后禁止裸 cp/copy2 备份**（会丢 -wal 未合并页）。一切备份走
  `sqlite3 <db> ".backup '<dst>'"`（shell）或 `conn.backup()`（Python）。
- 备份轮转**只有一套政策**：`scripts/prune_backups.py`（日备成对保留 + 月度锚点永久 +
  ipo 备份留 2）。ZSXQ run_daily_extract.sh 里的 `ls -t | tail | rm` 旋转已删除——
  它曾把 6/7 月锚点和全部 ipo 备份误删。
- schema 变更：目前无迁移机制，靠"新列必须兼容老库"纪律；中期计划见 §5。

### 2.5 文件写入

- 会被看门狗 TERM/KILL 的管线里，"产物文件存在即已完成"判据的产物**必须原子写**
  （tmp + `os.replace`），读取端必须容忍坏 JSON（跳过并删除，等下轮重做）。

---

## 3. 数据存储

```
facts.db      facts        时序事实账本(双时态 valid_at/observed_at, 状态机 active/superseded/disputed)
structure.db  relations    typed 产业链边 + 多篇投票
entities.db   entities+aliases  实体归一(canonical_id + 别名 + merged_into)
sentiment.db  sentiment(+raw)   舆情轻 lane，D 级隔离
vectors_bge.db vectors     语义向量(fact_id → BLOB)，与 facts 弱关联(无外键，靠 build 清孤儿)
vectors_bge.db.mat.npy / .ids.json   语义矩阵磁盘缓存(派生物，memmap 秒开；指纹=主文件 mtime/size+条数，
                           失配自动从 BLOB 重建；可随时删除，不进备份)
facts.db      facts_fts + fts_map + fts_meta   FTS5 关键词索引(2-gram + bm25)：upsert 同事务写，
                           `tkb fts build` 日常对账(补缺+清孤儿)；fts_meta.built 未置位时 search 走 LIKE 降级；
                           索引文本 = claim+object+subject+extra.entities(2026-08-26 起,多实体可按次要实体名召回)
facts.db      background_log   分流判 background 的 finding 原文留痕(2026-08-26 v3)：不进 FTS/向量、不参与检索，
                           只供审计与规则演进回填；此前 background 直接丢弃、无痕不可审计
facts.db      doc_claim        (doc_id, ckey=blake2b(归一 claim), fact_id) 判重索引(2026-08-27)：同一来源文档同一句
                           只能有一行——upsert 入口命中即返回旧行(superseded 也算存在、不复活；分挂不同证券码的
                           刻意拆分例外)；插入/合并自动登记；改写 fact_id 的脚本必须 remap；`tkb docclaim build|status`
facts.db      facts.ingested_at  入库时刻(2026-08-26 起首插写入，存量留空)：只做回溯与 valid_at≤ingested_at 校验，永不排序
facts.db      facts.valid_at   经 dates.clean_date 闸口(ISO 日历合法、[2000, 明天])，否则置空=未知；研报卡日期来源见
                           extra.valid_at_source / 卡片 date_source(filename / zsxq_post / pdf_creation / pdf_mod)
hypotheses/H*.md           假设账本(纯文件)
```

分流类别(classify.py，2026-08-26 v3)：hard_fact / quant_fact / structure / **view** / background。
view = 有非垃圾实体、无硬数字/硬谓词的定性论断(predicate HAS_VIEW，成色=信源基线降一档，恒 unverifiable)，
入 facts 可检索、进证据链与情绪面，但 ask 结论头条跳过它(`_top_conclusion`)。数值兜底不再要求正文自带年份
(时间锚=卡片日期→valid_at)。规则单调放宽：原判 hard/quant/structure 的判定与 fact_id 不变，
存量回填 `scripts/backfill_background.py` 按"fact_id 已存在即跳过"补齐此前被丢的部分(不走全量 re-ingest，
因 upsert 合并路径会复活 superseded 行)。归因与方案：docs/BACKGROUND_FIX_PLAN_20260826.md。

已知的结构性约束（当前接受、中期偿还）：
- fact_id 是语义派生哈希 → predicate/主体一变 id 就变，规则演进需 requalify 手术；
- facts_store.search 已改 FTS5(2026-08-25，docs/RECALL_FIX_PLAN_20260825.md)；LIKE 只作降级路径。
  绕过 FactsStore 的 raw-SQL 治理脚本会让索引漂移，靠日常 `tkb fts build` 对账兜底；
- ask 快路径按实体覆盖度 + 短语型判定(FAST_PATH_MIN_FACTS / is_pseudo_company)；零/寡事实的真公司
  company: 仍会被 _locate_entity 锚到并切发现模式(出声)，这是设计行为不是 bug；
- EntityRegistry.merge 不回写 facts/relations，事实侧靠治理脚本 _reattribute、关系侧靠
  scripts/fix_relation_merged_refs.py 事后对账(2026-08-25 起)；
- ask._locate_entity 全量别名进 Python 匹配（17 万级，问答固定开销）。

---

## 4. 运维管线

```
launchd com.kbsync.daily (01:00)
  └─ ~/kb_sync/sync_all.sh (run_step: rc 检查+重试一次+watchdog)
       ├─ ZSXQ/ima 同步 → 抽取 → 语义索引
       ├─ run_announcements.sh → announcements_to_kb.py(昨日公告→A级) + ipo daily_run.py
       ├─ run_ir_qa.sh → ir_qa.py + ir_qa_to_kb.py(互动平台→A级)
       └─ 实体治理(merge_*/clean_entities, dry-run 默认, 备份前置)
launchd com.tkbprune.daily (12:00) → prune_backups.py(唯一轮转政策)
```

退出码协议见 §2.2。所有国内数据源请求必须显式直连
（`ProxyHandler({})` 或建 opener 时排除环境代理），不依赖"launchd 环境恰好干净"。

---

## 5. 中长期路线（本轮明确不做，防止范围失控）

1. **主键与语义解耦**：facts 换自增代理主键，dedup_key 保留 UNIQUE 独立演进；
   引入 `PRAGMA user_version` + migrations/ 目录，把 requalify 类脚本收编为编号迁移。
2. ~~FTS5 替代 LIKE 全表扫~~(2026-08-25 已做)；别名定位改"query 提取候选子串→索引查询"。
3. structure_store 与 facts_store 的乐观重试逻辑抽共享基类（当前同构复制已开始漂移）。
4. 反证闭环（contradict）与 D 级升级闸（sentiment promote）目前是纸面功能，
   待接线时先补调用方设计。
5. ~~注册表卫生~~（2026-08-25 已做）：① ingest 闸门 `entity_quality.is_pseudo_company`（ingest_card 里
   company→concept 降级，ask._fast_path 同规则拒快路径）；② 存量 `scripts/retype_pseudo_companies.py`
   改型 3,116 个 company:短语 → concept:（重挂 5,791 事实 / 6,029 关系）。**没有**整批清 74,606 个零事实
   company——实测它们绝大多数是真公司（数库科技/丸红/Cologix…，事实挂在别处），且 3,056 个被关系引用。
   另修 `scripts/fix_relation_merged_refs.py`：历史 merge 从不回写 relations，8,413 条边悬空指向旧 cid。
5. web 内嵌 340 行 HTML/JS 拆独立资源文件；web 端 ask 补 auto_verify 与 CLI 对齐。

---

## 6. 测试策略

- `run_tests.py` 收集 `tests/` + `scripts/ipo_prospectus/test_ipo_prospectus.py`。
- 原则：mock 打在 seam 上（HTTP 层 _http、verify 钩子、complete 注入），不 mock 被测物。
- 每晚自动 `--apply` 写生产库的治理脚本，其纯函数核心必须有测试钉住：
  `_reattribute` 主键不变量、prune_backups 三个规划器、announcements is_substantive。
- 联网测试一律 `TKB_LIVE=1` 门控。

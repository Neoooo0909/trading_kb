# tkb ask 召回缺口 · 根因与修复方案（2026-08-25）

> 状态：**已审核通过并实施**（2026-08-25 夜，P0-A / P1-D / P0-C / P0-B 四项全部上线，见 §7 实施记录与验收结果）。
> 所有数字来自对生产库 `data/facts.db`（active 1,105,237）的实测，复现脚本在 `docs/recall_fix_scratch/`。

---

## 0. 一句话结论

两个案例（球硅 / 燃气轮机）的直接死因**不是**排序权重，而是 `_locate_entity` 把查询锚到了注册表里的**伪公司实体**（`company:hbm先进封装`、`company:数据中心自备电源`，各只有 1–2 条自有事实），`_is_security()` 把一切 `company:` 当证券走"精准快路径"——**语义召回根本没跑**，跨证券别名过滤又把其余候选全部丢掉，最后只剩那 1–2 条。
在此之下还有两层独立缺陷：SQL LIKE 预筛 0/400 命中（且已无法靠改 LIKE 救：单 token 全表扫 14–28s）；LLM 合成材料 `material[:8000]` 只看得到证据链前 ~70 条。

修法三件套：**① 路由按实体覆盖度而非类型前缀**（20 行，立刻修好两个案例）；**② FTS5 bigram + BM25 替换 LIKE**（原型已验证：建库 47s / 198MB / 查询 0.05–0.16s，球硅命中排第 1/3/5/20）；**③ 放宽 LLM 材料窗口**。附带一个 P1：语义向量矩阵磁盘缓存，否则语义成为默认路径后每次 ask 冷启动 3 分钟。

---

## 1. 根因（按影响排序，均已实测）

### R0 · 实体路由错锚（主因，本次新发现）

`ask.py:213` `_locate_entity("球硅 HBM 先进封装 需求")` → `company:hbm先进封装`（注册表 type=company，source=ingest，仅 1 条 social_research 事实）。
`ask.py:227` `want_sem = (cid is None or not _is_security(cid))`，而 `_is_security()`（`ask.py:432`）对 `company:` 前缀一律返回 True → `want_sem=False`。
`ask.py:224` `ent_aliases = registry.aliases_of(cid)` 非空 → `_rank_facts` 里"跨证券噪音过滤"（`ask.py:306`）把**所有非本实体且正文不含 `hbm先进封装` 的事实丢弃**。
结果：池里 LIKE 灌进来的 400 条几乎全被过滤，语义那路没跑，最终只剩"HBM先进封装 DRIVES 半导体封测"这 1 条——与用户看到的输出逐字吻合。燃气轮机案例同构：锚到 `company:数据中心自备电源`（2 条事实），只剩"数据中心自备电源 DRIVES 阳光电源"。

注册表规模（`entities.db`，type=company 且未合并）：

| 指标 | 数值 |
|---|---|
| company 实体总数 | **109,346**（stock 43,509 / concept 146,260） |
| 自有 active 事实 = 0 | **74,606** |
| = 1 | 19,812 |
| ≥ 10 | 4,016（NVIDIA 4,947 / 台积电 4,054 / SK海力士 …——这些快路径是对的） |
| 带 stock_code | 47 |
| 名字含 需求/电源/封装/数据中心/板块/概念/产业链… 的（粗筛） | 2,904（"AIDC用电需求""800V电源架构""AI PCB需求"…） |

含义：**任何查询只要子串命中这 10 万个名字之一，就被劫持进快路径并静默丢弃其余一切**。这是一个面，不是两个点。

**反证检查**（为什么不是排序问题）：把语义强制打开、其余逻辑不动（诊断脚本 `diag.py`），球硅四条精确事实按 `_rank_facts` 分数排在 **#2 / #3 / #24 / #81**（rel 1.0 + sem 0.76 + rec 1.0），`_diversify_by_kind` 后 **#8 / #11 / #45 / #99**——前两条稳进 LLM 窗口。燃气轮机案例语义 120 条里 101 条相关，前 20 名里 16 条是燃机。**排序与多样化本身工作正常**，转来的"② 语义捞到却没进结论"在真实链路里的解释就是 R0（语义没跑），不是权重。

### R1 · LIKE 预筛结构性失效（转来的 ①，证实并补充）

`facts_store.py:252 search()`：token OR 拼 LIKE，`ORDER BY rowid DESC LIMIT 400`。实测：

| 查询 | 400 条中真命中 | 耗时 |
|---|---|---|
| 球硅 HBM 先进封装 需求 | **0** | 0.07s（"需求" DF=63,346，最近 400 条全是它） |
| 燃气轮机 数据中心 自备电源 受益标的 | 6 | 0.04s |
| 球硅（单词全表扫） | 243 | **27.9s** |

单 token DF：球硅 243 / 先进封装 3,541 / 封装 7,166 / HBM 8,568 / 需求 63,346；每个 token 单独 LIKE 全表扫 **14–22s**。
结论：**"按稀有 token 先查、再交集/排序"的 LIKE 改良路线不可行**（每 token 一次 15s 全扫）。`_STOP_GRAMS` 手工黑名单也治不了长尾高频词（需求/技术/设备…）。ARCHITECTURE §5.2 早已把 FTS5 列为中期偿还项，库从 18.7 万涨到 110 万后到期了。

### R2 · LLM 材料窗口截断

`llm.py:138 synthesize_answer`：`material[:8000]`。证据链每行 ~90–110 字符 → LLM 只看到 **前 ~70–80 条**；六段骨架本身"证据链不截断"是对的，但合成层看不见后面。候选池 500+ 条时，凡排在 80 名以外的一律对 LLM 不存在。

### R3 · 语义冷启动（次要，但 ① 落地后会暴露）

`semantic.py:222 _load_matrix`：每个进程从 `vectors_bge.db` 逐行读 1.1M 个 BLOB 再 `vstack`（2.26 GB）。诊断脚本里首查 **216s**、次查 2.7s。CLI 每次 `./tkb ask` 是新进程——语义一旦成为常规路径，这就是每问 3 分钟。
（拆分计时：见 §6 附录，脚本 `semantic_cold.py`。）

---

## 2. 方案

### P0-A · 路由按覆盖度（`ask.py`，约 20 行）

把 `_is_security(cid)` 的两个用途拆开：
- **实体优先级**（`_locate_entity` 里"证券优先于概念"）保持不变；
- **是否走精准快路径**改为 `_fast_path_ok(cid)`：
  `SH/SZ/BJ 码` **或** `company:` 带 `stock_code` **或** 自有 active 事实数 ≥ **10**（`facts.query(canonical_id=cid, limit=10)` 取 len，一次索引查询）。
- 不满足时：`want_sem=True`，`ent_aliases=∅`（不做跨证券过滤），`warnings` 追加 `"锚到低覆盖实体 <display_name>(N 条),已切发现模式:关键词+语义加权"`——出声，不静默。
- 阈值 10 的依据：4,016 个 company 保留快路径（NVIDIA/台积电/长鑫这类真公司），94,418 个零/寡事实伪实体不再劫持。`use_semantic` 显式参数仍最高优先。

效果：两个案例立即修复（诊断脚本已等价验证）。不改任何权重、不改注册表。

### P0-B · FTS5 bigram + BM25 替换 LIKE 预筛（`facts_store.py` + 新 `fts.py` + `cli.py`）

**设计**
- 表放在 `facts.db` 内（与 facts 同库同事务，WAL 下原子）：
  ```sql
  CREATE TABLE fts_map(id INTEGER PRIMARY KEY, fact_id TEXT UNIQUE);
  CREATE VIRTUAL TABLE facts_fts USING fts5(grams, content='', tokenize='unicode61');
  ```
  `grams` = `content_grams(claim+object+subject)` 用空格连接（中文 2-gram + 英数词，与现有 `models.content_grams` 同一基元，检索/冲突消解口径一致）。rowid = `fts_map.id`，**不依赖 `facts.rowid`**（TEXT 主键表的 rowid 会被 VACUUM 重编，`fts_map` 按 fact_id 对账即可）。
- 查询：query 的全部 gram/词 **OR** 拼 `MATCH`，`ORDER BY bm25(facts_fts) LIMIT 400`，再按 fact_id 回表过滤 `status IN ('active','disputed')`。**截断发生在 BM25 相关性排序之后**——这是对转来问题的正面回答。IDF 自动把"需求/技术"这类高 DF 词压到近零权重，`_STOP_GRAMS` 保留在查询侧作双保险即可，不再是主要防线。
- 为什么不用 FTS5 自带 `trigram` tokenizer：它对 <3 字符的子串一律不命中，而"球硅/燃机/封装/HBM"这类 2 字关键词正是 A 股语料的主力。
- 同步：`FactsStore.upsert` 新插入分支同事务 `INSERT fts_map + INSERT facts_fts`；`supersede/mark_disputed/patch_extra` 不改正文，无需动；合并分支只改成色/来源，无需动。
- 对账：新增 `./tkb fts build`（增量补缺 + 清孤儿，与 `tkb semantic build` 同模式），挂到 `run_daily_extract.sh --tail-only` 的 E 段之后。绕过 `FactsStore` 的 raw-SQL 写者（`scripts/requalify_quant.py` / `clean_entities.py` / `ZSXQ/kb_adapter/rebuild_clean.py` 的 DELETE / `UPDATE fact_id`）造成的漂移由对账兜底；查询侧回表过滤保证漂移只浪费名额、不出错。
- 降级：`facts_fts` 不存在或 MATCH 抛错 → 回退旧 LIKE 路径，并 `stderr` 出声（§2.2 出声原则），`warnings` 注明"关键词召回已降级为 LIKE"。
- 首建：`./tkb fts build` 全量 1.1M 行，原型 **47s / 198MB**；须避开 01:00 `com.kbsync.daily`，建前 `.backup/facts.db.<ts>`。

**原型实测**（`fts_proto.py`，独立 scratch 库，未碰生产库）

| 查询 | 400 条中真命中 | 命中位置（前几个） | 耗时 |
|---|---|---|---|
| 球硅 HBM 先进封装 需求 | **6**（LIKE: 0） | 1, 3, 5, 20 | 0.16s |
| 燃气轮机 数据中心 自备电源 受益标的 | **348**（LIKE: 6） | 2–10 连续；杰瑞 14.65 亿大单排 #5 | 0.08s |
| 银轮股份 | 153 | 1–5 | 0.05s |
| 精智达 存储测试设备 进展 | 83 | 1, 2, 6, 9… | 0.05s |

**现有测试兼容性**：`test_search_not_limited_to_2000`（稀有词 鳑鲏鱼）、`test_search_with_like_wildcard_chars`（`毛利率50%以上`：`50` 成英数 token，`%` 不进 token）、`test_search_stop_gram_不影响整词查询`（整词 `股份回购` → gram 份回 稀有、高分）三条语义不变；tmp fixtures 经 `FactsStore.__init__` 自动建表。

### P0-C · LLM 材料窗口（`llm.py` 一行 + `config`）

`material[:8000]` → `config.SYNTH_MATERIAL_CHARS`，默认 **24,000**（≈ 前 220 条证据；Kimi/DeepSeek/Sonnet 三档模型上下文都远超）。C 级情绪段已前置，不再被证据链尾部挤掉。

### P1-D · 语义矩阵磁盘缓存（`semantic.py`）

`_load_matrix` 首次从 BLOB 读完后 `np.save` 到 `vectors_bge.mat.npy` + `ids.json`，指纹 = 现有 `_db_version()`；后续进程 `np.load(mmap_mode='r')`。预期冷启动从 ~200s 降到秒级（实测数字见 §6）。`tkb semantic build` 结束时顺手重写缓存。这是 P0-A 把语义变成常规路径的必要配套，否则用户会把 3 分钟归咎于"修坏了"。

### P2-F · 注册表卫生（本轮不做，**拆两条分别登记，① 优先于 ②**）

实测伪实体是**持续增产**的：自 `entities.db.bak.20260810` 起 15 天新增 company 实体 **10,817** 个，其中零事实 6,880（64%）、单事实 3,415（32%）——**96% 是零/寡事实**，约 720 个/天（"全球AI基础模型市场""日本韩国动力煤需求""AI芯片厂商"…）。清存量是治标，闸门才是止血。

- **① ingest 闸门（优先）**：`ingest._kind_to_type("company")` 前加闸——名字命中 需求/电源/封装/板块/概念/产业链/市场/景气/产能 等尾词或 `is_garbage_entity` → 降为 `concept`，不再以 `company:` 登记；
- **② 存量清理**：对 74,606 个零事实 company 跑现有实体治理 lane（dry-run 默认、备份前置）。
- P0-A 已让它们不再劫持路由（只是仍会被 `_locate_entity` 锚到并触发"切发现模式"告警），因此两条都可以排期做，但 ① 别拖。

### 对转来候选方向的判定

| 候选 | 判定 | 理由 |
|---|---|---|
| 给语义结果保底名额 | 不做 | 治标；真实链路里语义根本没跑（R0），跑了排序就是对的 |
| 预筛改按 token 相关性排序（LIKE） | 不可行 | 单 token 全表扫 14–28s |
| 扩展 `_STOP_GRAMS` | 不做为主线 | 长尾高频词无穷尽，BM25 IDF 是同一件事的通解 |
| 调 `_rank_facts` 权重 | 不做 | 实测语义开启时目标事实排 #2/#3，权重无病 |
| FTS5/BM25 替代 LIKE | **做**（P0-B） | 原型已验证，ARCHITECTURE §5 既定方向 |

---

## 3. 验收标准

1. `./tkb ask "球硅 HBM 先进封装 需求"`：证据链前 12 含 ≥ 3 条球硅事实；`⚠ 检索告警` 出现"切发现模式"；LLM 结论不再说"没有球硅"。
2. `./tkb ask "燃气轮机 数据中心 自备电源 受益标的"`：杰瑞股份 14.65 亿大单 / 东方电气 G50 / 潍柴 UL 认证 进前 20。
3. 回归：`python3 run_tests.py` 195 绿；新增 5 个用例：
   - 低覆盖 `company:` 实体 → 发现模式（want_sem=True、无别名过滤、有告警）；
   - 高覆盖 company / 真股票码 → 快路径不变；
   - FTS：高频词 + 稀有词混查，稀有词事实排前；
   - FTS 对账：raw DELETE 后 `fts build` 清孤儿、raw INSERT 后补缺；
   - FTS 缺表 → LIKE 回退且出声。
4. 性能：`facts.search` < 0.3s；ask 端到端（语义开、含模型加载）冷启动 < 15s（P1-D 后）。
5. 快路径案例不回归：`./tkb ask "精智达"`、`"银轮股份"`、`"长鑫存储"` 输出与修前一致（跑 `tests/test_retrieval_regression.py` 全套）。

---

## 4. 实施顺序与风险

实施顺序（评审意见采纳：P0-A 与 P1-D 必须同批上线，否则语义成常规路径后每问 210s）：**P0-A → P1-D → P0-C → P0-B → 文档**。

| 步 | 改动 | 回滚 | 风险 |
|---|---|---|---|
| 1. P0-A | `ask.py` + `config.py` + `entity_registry.get()` | git revert / `.backup/*.20260825_230441` | 无库改动；阈值 10 若误伤真公司只是多跑一次语义（慢不错） |
| 2. P1-D | `semantic.py` | 删 `data/vectors_bge.db.mat.npy` + `.ids.json` | 磁盘 +2.3GB（派生物：`prune_backups` 只认 `*.db.bak.*`，`_db_backup` 只走 sqlite backup API，**不进备份**）；指纹失配自动重建 |
| 3. P0-C | `llm.py` + `config.py` | 同 1 | token 成本上升 ~3×，仅 USE_LLM 时 |
| 4. P0-B | `facts_store.py` / `cli.py` / `run_daily_extract.sh`；`./tkb fts build` 首建 | `DROP TABLE facts_fts, fts_map, fts_meta` + 代码回退；建前备份 `.backup/facts.db.bak.pre_fts_<ts>`（**手工命名，prune 不会自动删，验收后手删**） | 建表期间写锁 ~1 分钟，**避开 01:00**；facts.db +~200MB → 日更备份成对 ×3 天 + 月锚 ≈ +1GB，`DAILY_KEEP_DAYS=3` 暂不调 |
| 5. 文档 | `docs/ARCHITECTURE.md` §3 约束表 + §5 划掉 FTS5 一项、新增 §5.5 注册表卫生两条 | — | — |

项目惯例照做：改前 `.backup/<文件名>.<时间戳>`；改运行中脚本用 `mv` 原子替换；每步后跑 `run_tests.py`。

---

## 5. 复现命令

```bash
cd ~/trading_kb
# R0:实体锚定与路由(当前行为)
PYTHONPATH=src python3 -c "
from trading_kb import config; from trading_kb.cli import EntityRegistry, FactsStore, StructureStore
from trading_kb.ask import AskEngine, _is_security
e=AskEngine(EntityRegistry(config.ENTITY_DB), FactsStore(config.FACTS_DB), StructureStore(config.STRUCTURE_DB))
for q in ['球硅 HBM 先进封装 需求','燃气轮机 数据中心 自备电源 受益标的']:
    cid=e._locate_entity(q); print(q,'->',cid,'| 快路径' if _is_security(cid) else '| 发现模式', '| 自有事实', len(e.facts.query(canonical_id=cid, limit=50)))"
# R1:LIKE 预筛 0/400 + 单 token 全扫耗时
#   见转来消息的复现命令;单 token: SELECT count(*) FROM facts WHERE claim LIKE '%球硅%'  (≈15-28s)
# 完整分数分解 / FTS 原型 / 冷启动拆分:
#   scratch: diag.py · fts_proto.py · semantic_cold.py(会话 scratchpad,已随本方案归档到 docs/recall_fix_scratch/)
```

---

## 6. 附录 · 语义冷启动拆分实测（`docs/recall_fix_scratch/semantic_cold.py`）

| 阶段 | 耗时 |
|---|---|
| 后端探测（`_auto_backend`） | 1.7s |
| `_load_matrix`：sqlite BLOB 逐行读 1,105,237 × 512 → vstack（2.26 GB） | **210.6s** |
| 首次 `search`（含 ONNX 模型加载 + 矩阵点积） | 9.8s |
| `np.save` 矩阵到 .npy | 1.7s |
| `np.load(mmap_mode='r')` + 一次点积（冷读盘） | **6.9s**（页缓存热后 <1s） |

结论：P1-D 把每进程 ~210s 压到 ~7s；剩余大头是模型加载 ~8s，属固定成本。P0-A 让语义成为常规路径后，P1-D 是必须的配套，不是可选优化。


---

## 7. 实施记录与验收结果（2026-08-25 23:00–23:30）

**改动**（改前备份 `.backup/<文件>.20260825_230441`；日常脚本备份 `~/ZSXQ/kb_adapter/.backup/run_daily_extract.sh.20260825_230*`，mv 原子替换）

| 项 | 文件 | 要点 |
|---|---|---|
| P0-A | `ask.py` `_fast_path()` / `config.FAST_PATH_MIN_FACTS=10` / `entity_registry.get()` | 快路径 = 真上市码 ∨ 带 stock_code ∨ 自有事实 ≥10；否则语义开 + 不做别名过滤 + 告警"锚到低覆盖实体…已切发现模式"。顺带修了 `ask()` 内局部 `from . import config` 遮蔽模块名的 UnboundLocalError |
| P1-D | `semantic.py` | `.mat.npy`+`.ids.json` 磁盘缓存，指纹 = 主文件 mtime/size + 条数（故意不含 -wal）；`build()` 结束 `wal_checkpoint(TRUNCATE)` 后顺手刷新缓存 |
| P0-C | `llm.py` / `config.SYNTH_MATERIAL_CHARS=24000` | 环境变量 `TKB_SYNTH_MATERIAL_CHARS` 可调 |
| P0-B | `facts_store.py`（`facts_fts`/`fts_map`/`fts_meta`、`_search_fts`、`fts_build`、`fts_status`、LIKE 降级 `_search_like`）/ `cli.py`（`tkb fts build\|status`）/ `run_daily_extract.sh` F 段 | 空库自动 built；存量库须 `fts build`；upsert 同事务写索引；raw-SQL 漂移靠日常对账 |
| 测试 | `tests/test_recall_fix.py` 7 例 | 全套 **196 passed, 2 skipped** |
| 文档 | `docs/ARCHITECTURE.md` §3 约束 / §5.2 划掉 / §5.5 注册表卫生两条 | |

**首建与踩坑**
- `./tkb fts build` 生产库全量：1,105,237 条，**6.5 分钟**（比原型 47s 慢在每行一次 `fts_map` 点查；一次性成本，日常增量为秒级），facts.db 1.24 → **1.45 GB**（+208MB）。建前备份 `.backup/facts.db.bak.pre_fts_20260825_231031`（1.24GB，**手工命名，prune 不会自动删，验收无误后请手删**）。
- 上线后首测 `search` 竟要 **8–10s**（原型 0.05–0.16s）：定位为回表 SQL `fact_id IN (…) AND status IN (…)` 被规划器选了 `idx_facts_status`（扫全部 110 万 active 行再过滤 IN），改 `+status` 禁用该列索引后强制走主键 → **0.05–0.61s**。教训：多列条件的点查要看 `EXPLAIN QUERY PLAN`，别信"有主键就走主键"。
- 语义矩阵缓存首建：第一次 ask 4m37s（BLOB 装载 + 落盘 2.26GB），此后每次 ask 冷进程 **14s**（其中模型加载 ~8s）。

**验收（`TKB_USE_LLM=0`，全文见 `docs/recall_fix_scratch/acceptance/`）**

| 用例 | 结果 | 判定 |
|---|---|---|
| ①「球硅 HBM 先进封装 需求」 | 告警"锚到低覆盖实体 company:hbm先进封装(1 条)…切发现模式"；证据链 482 条，球硅事实在 **F5 / F8 / F12** / F131 / F218…（FTS 还新捞到一条 B 级"亚微米级球硅需求增幅 50–80%"）；端到端 14.9s | ✓ 前 12 含 3 条 |
| ②「燃气轮机 数据中心 自备电源 受益标的」 | 燃气轮机事实占据 F1–F16 几乎全部；**杰瑞 14.65 亿大单 F2、东方电气 F7、潍柴 F20**、西门子能源 F23；14.5s | ✓ |
| ③ 回归 | 196 passed / 2 skipped | ✓ |
| ④ 性能 | `facts.search` 0.05–0.61s；ask 冷进程 14–15s（发现模式）/ 4–5s（快路径） | ✓ |
| ⑤ 快路径不回归 |「精智达」无告警、F1–F12 全为精智达、3.98s；「长鑫存储」（company: 但 ≥10 条）无告警、4.69s | ✓ |

LLM 合成层（`TKB_USE_LLM=1`）本轮未跑（避开会话残留 KIMI key / 403 判死问题），材料窗口改动为纯上限放宽，无逻辑分支。

**未做 / 待办**
- P2-F ① ingest 闸门（优先，伪实体每天 +720 个）与 ② 存量清理（74,606 个）——登记在 ARCHITECTURE §5.5。
- 首建备份 `facts.db.bak.pre_fts_20260825_231031` 验收后手删；`DAILY_KEEP_DAYS=3` 暂不调。
- 下一次 01:00 `com.kbsync.daily` 跑完后，看 `~/ZSXQ/kb_adapter` 日志里 F 段 "FTS5 关键词索引对账" 是否正常（预期秒级，补缺 = 当晚新增条数，孤儿 = D 段治理删改数）。

### 7.1 追加（同夜 23:30–24:00，用户指示"没做的都做掉"）

- **LLM 端到端验收**（`./tkb ask` 默认 LLM 开，`env -u KIMI_API_KEY`）：结论直接围绕球硅——CoWoS 光罩→底填/塑封料、2027 高端球硅需求 1.64/0.88 万吨、联瑞新材/华海诚科/壹石通映射，不再说"材料中没有球硅"；62s。
- **增量 `fts build` 实测 11.5s**（读 2×1.1M id 对账，补缺 0）——今晚 F 段预期同量级。
- **P2-F ① ingest 闸门**：`entity_quality.is_pseudo_company()`（泛化后缀 / 地域前缀+泛词 / 动词短语；含 有限/公司/集团/股份/合伙/Inc 的一律放行；"企业/投资"裸后缀与"美国+银行"故意不收，万业企业/粤海投资/美国银行是真公司）。接入 `ingest_card`（company→concept）与 `ask._fast_path`（短语型即使事实 ≥10 也拒快路径，堵"云厂商 76 条"这类残余劫持）。
- **P2-F ② 存量改型** `scripts/retype_pseudo_companies.py --apply`：候选 3,116 个（dry-run 抽样人工过目无真公司），重挂事实 5,791、关系 6,029，85s；三库热备 `.backup/*.bak.retype_20260825_233439`（手工命名，prune 不删，确认后手删）。改后 `_locate_entity` 把两案例锚到 `concept:hbm先进封装` / `concept:数据中心自备电源`，无告警。**不清** 74,606 个零事实 company（实测多为真公司且 3,056 个被关系引用）。
- **遗留债修复** `scripts/fix_relation_merged_refs.py --apply`：历史 merge 从不回写 relations，8,413 条边悬空指向旧 cid（非本次造成）→ 改指 8,399（其中 8,359 与合并目标下已有的同边碰撞、并入 sources）、14 条自环删除，修后悬空 0、总边 153,075；备份 `structure.db.bak.relfix_20260825_233803`；另有 177 条事实同病，属 `clean_entities._repair_stale` 范畴未动。
- 向量：5,791 条 fact_id 变更 → 手动跑 `tkb semantic build` 补建（1,105,237/1,105,237，矩阵缓存由 build 顺手刷新）。补建前潍柴 UL 事实曾从 F20 掉到 F63（无向量），补建后复核回到 **F20**，两案例均无告警（`acceptance/p1_after_retype.md` / `p2_after_semantic_rebuild.md`）。
- 测试 **199 passed / 2 skipped**（P2-F 新增 3 例）。
- 首建备份 `facts.db.bak.pre_fts_*` 已删（21:56 日更成对备份是等价回滚点）。

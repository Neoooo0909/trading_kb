# 入库层 background 丢弃 · 根因与修复方案（2026-08-26）

> 状态：**已审核（按 §5 推荐执行）。代码已落地（2026-08-26 14:30，217 tests 绿），存量回填待用户定范围与时间窗。**
> 回填前排位/冷启动基线：`docs/background_fix_scratch/rank_before.md`（回填后用 `rank_snapshot.py` 再跑一次对照）。
> 所有数字来自对生产卡片目录与 `data/facts.db`（active 1,114,169）的实测，
> 复现脚本与 400 条 LLM 标注样本在 `docs/background_fix_scratch/`。
> 起因：星球 7 月起 18 帖提到晓程科技（SZ300139），`./tkb ask` 一条社媒内容都查不到；
> 4 条含晓程的 finding 全部在 `ingest.py::ingest_finding` 被判 background 后直接 `return`。

---

## 0. 一句话结论

`classify_finding` 的 background 兜底不是"定性背景"，而是**"规则没接住的一切"**：抽样 400 条被丢的
finding，LLM 分档只有 **10% 是真背景**，**60% 是带具体数字/事件的硬事实、27% 是有明确主体的定性论断**。
直接死因两条：① 数值路径要求**文本内自带年份**（`_YEAR_RE`），而卡片本身就带 `date`，社媒/研报里
"5.88 亿美元""20%""每机架 33 万颗"这类硬数字句 96% 被误杀；② 四类分流里**没有"有主体的定性论断"这一档**，
"晓程科技拥有海外金矿资产，利润弹性突出"这种句子在现行规则下**必然**落 background。
再叠加 ingest 把 background 直接丢弃（设计文档写的是"留原文"，从未实现），六到七成产出无痕消失。

修法：**分流加一档 `view` + 数值路径去掉年份门槛 + background 改"审计留痕"而非丢弃 + 一次性回填**。
预计社媒帖 lane 的丢弃率从 60% 降到 ~3%，库从 111 万涨到 ~185 万行（+65%），磁盘 +5.5GB，回填 4–5 小时。

---

## 1. 根因（按影响排序，均已实测）

### R0 · 生产路径就是纯规则，与 dc 的 `llm=None` 复现一致

`~/ZSXQ/kb_adapter/ingest_kb_cards.py:201` 构造 `ResearchIngestor(registry, facts, structure, critique_engine=...)`
**没有传 `llm_classify`**；`TKB_USE_LLM` 只在 `ingest.run_ingest()`（`./tkb add` 路径）生效。三个社媒 lane
（cards / cards_ima / cards_zsxq_research）全部走规则核心，`classify_finding(f, llm=None)` 就是生产行为。
（LLM 分流钩子 `llm.py:_CLASSIFY_PROMPT` 的四类定义同样没有"有主体定性论断"档，即使接上也会把它判 background。）

### R1 · 数值路径的"年份门槛"在社媒/研报 lane 是纯误杀

`classify.py:188` 第 4 组兜底：`_has_hard_number(raw) and _YEAR_RE.search(raw)` → hard_fact。
设计意图是"可证伪须有时间锚"，但**时间锚已经在卡片的 `date` 里**（Finding.source_date），规则却只认正文。

| 被判 background 且含百分比/金额、无年份 | 占 background | LLM 标签 |
|---|---|---|
| 社媒帖 lane | 28.7% | — |
| zsxq_research | 24.7% | — |
| ima | 28.2% | — |
| 合计抽样（400 条中命中 91 条） | — | **N 96% / Q 4% / B 0%** |
| 其中卡片也无日期的 48 条 | — | N 46 / Q 1 / B 1 |

即：**硬数字本身就是 96% 精度的可证伪标记，年份门槛没有增加精度、只减少召回**。
同类：硬谓词路径 `_match_hard_predicate and (_DATE_RE or f.numbers)`——"三星电子已开始量产用于 Vera Rubin 的存储驱动器"
命中"量产"但无日期无数字 → background；"Truist 给予 EQIX/DLR/AMT 买入初始评级"命中"评级"但无数字 → background。

### R2 · 四类分流缺"有主体的定性论断"档

`Category = hard_fact | structure | quant_fact | background`。晓程那条：单实体、主语明确、可跟踪，
不含数字、不含订单/产能/评级谓词、不含产业链词 → 五组判定全不命中 → 兜底 background。这不是规则漏洞，
是**分类体系没有这个格子**。抽样里这一档（有主体、无硬数字）占 background 的 31%，LLM 标签 Q 57% / N 25% / R 5% / B 13%。

### R3 · "background = 留原文"从未实现，丢弃无痕

`classify.py` 模块 docstring：`background : 定性/预测/背景 → 留原文,不入图`；`ingest.py` 头注释同。
实际 `ingest_finding`：`if cat == "background": report.background += 1; return`。没有任何表、日志、
文件记录被丢的 claim；`report.background` 只是计数。事后无法审计、无法回填、无法回答"星球明明说了为什么库里没有"。
`_ingest_structure` 里不足两端实体的 structure finding 同样计入 background 后丢弃。

### R4 · 历史：2026-08-01 v2 已经修过一次同类病，但社媒 lane 又涨回去了

v2 docstring 记录：研报 70.8% 落 background → 修到 42.1%（研报）/ 58.4%（社媒）。本次实测社媒帖 lane **60.4%**、
zsxq_research 49.5%、ima 53.4%、report_lab（券商研报）24.8%。v2 补的是英文谓词/财务/评级词典，
没动"年份门槛"和"缺定性档"这两个结构性缺口，社媒语料（短句、少年份、多观点）正好全踩在这两个缺口上。

### R5 · 次要：`_pick_subject` 只取首实体

dc 已抽样：entities≥2 的 finding 占 46.3%，非首实体提及 3661 次被丢。晓程 3 条多实体 finding 即便过闸也会挂在
浩通科技/贵金属名下。因 claim 正文含"晓程"，FTS 仍能召回、跨证券别名过滤也能放行，但拿不到 `ent_hit=+2` 的排序加分。

---

## 2. 抽样归因

### 2.1 各 lane background 率（规则 v2.1，随机抽卡，`sample_bg.py`）

| lane | 抽卡 | findings | background | hard_fact | structure | quant |
|---|---|---|---|---|---|---|
| 社媒帖 cards | 1500 | 8,588 | **60.4%** | 29.5% | 9.8% | 0.3% |
| cards_zsxq_research | 800 | 23,769 | **49.5%** | 44.8% | 5.1% | 0.6% |
| cards_ima | 800 | 15,825 | **53.4%** | 40.3% | 5.8% | 0.5% |
| report_lab/cards（券商研报） | 300 | 4,766 | 24.8% | 34.3% | 4.2% | 36.7% |

### 2.2 background 启发式分桶（26,594 条）

| 桶 | 定义 | 社媒帖 | research | ima |
|---|---|---|---|---|
| STOCK_NUM | 有股票/公司实体 + 数字 | 27.6% | 21.0% | 23.0% |
| STOCK_QUAL | 有股票/公司实体、无数字 | 31.4% | 19.7% | 17.2% |
| ENT_NUM | 有其他实体 + 数字 | 18.4% | 20.2% | 23.4% |
| ENT_QUAL | 有其他实体、无数字 | 17.6% | 14.7% | 16.7% |
| NOENT_NUM | 无实体 + 数字 | 2.7% | 12.2% | 10.7% |
| NOENT_QUAL | 无实体、无数字 | 2.3% | 11.6% | 8.6% |
| BOILERPLATE | 黑名单命中 | 0% | 0.7% | 0.3% |

社媒帖 lane：**95% 的 background 带实体，59% 带具体股票/公司**。"无主体套话"只占 5%。

### 2.3 LLM 分档（DeepSeek，分层抽 400 条，`tag_llm.py` → `bg_tagged.jsonl`）

档位：N=带数字/事件可证伪硬事实 · Q=有主体定性论断 · R=结构关系 · B=真背景

| lane | n | N | Q | R | B |
|---|---|---|---|---|---|
| 社媒帖 | 150 | 59% | 26% | 3% | 13% |
| zsxq_research | 125 | 55% | 28% | 3% | 14% |
| ima | 125 | 65% | 27% | 3% | 5% |
| **合计** | 400 | **60%** | **27%** | 3% | **10%** |

桶 × 标签：STOCK_NUM / ENT_NUM 各 90% N；STOCK_QUAL 55% Q + 34% N；ENT_QUAL 61% Q；NOENT_QUAL 69% B。
对抗复核：LLM 把 2 条"评级分布披露"标成 N（实为合规模板，黑名单判对了）；B 档抽查 12 条均为
"本周矿业股表现强劲""商品价格具备周期性"类，确为真背景。N 档在 *_QUAL 桶里的 36 条多为"已量产/入选名单/达成合作/拟 H 股上市"
这类**无数字的已发生事件**——是 R1 的硬谓词分支被时间门槛卡住。

### 2.4 候选规则在样本上的覆盖与精度

| 规则 | 覆盖(26.6k) | 400 条标签分布 | 结论 |
|---|---|---|---|
| A1 硬数字(百分比/金额)，不要求年份 | 20.4% | **N 96%** / Q 4% | 采纳 → hard_fact |
| A2 硬谓词/评级/财务词 + 卡片日期 | 7.3% | N 42% / Q 50% / B 8% | 精度不够，**不单独进 hard_fact**，随 C 进 view |
| B numbers 字段(剔纯日期值) + 有实体 | 24.9% | N 86% / Q 10% / B 2% | 采纳 → hard_fact |
| C 有实体(非垃圾实体)、其余定性 | 31.0% | Q 57% / N 25% / R 5% / **B 13%** | 采纳 → **view**（新档） |
| D 其余（无实体无数字） | 16.1%（社媒帖仅 3%） | B 43% / N 28% / Q 22% | 不入库，**审计留痕** |

按 A1+B+C 保留、D 留痕：400 条里保留 344 条，丢弃的 56 条中 B 24 / N 16 / Q 12——
D 里的 N/Q 多是 entities 为空但 claim 有主语的英文宏观句（"Equity funds saw inflows of US$26bn"），
`_pick_subject` 的 `attribute_subject` 兜底可再捞一部分，本轮不追。

---

## 3. 方案

### P0-A · `classify.py` 规则 v3（两处，单调放宽——不改任何现有 hard/quant/structure 的判定）

1. **第 4 组数值兜底去掉年份门槛**：`elif _has_hard_number(raw): cat = "hard_fact"`。
   时间锚改由 `Fact.valid_at = f.source_date` 承担（已是现状字段）；正文无年份、卡片也无日期的，
   `valid_at=""` 与现有 51% 无日期研报卡的硬事实同口径。
2. **新增 `view` 档**（`models.Category` 加 `"view"`）：五组全不命中时，若 finding 有至少一个非垃圾实体
   （复用 `entity_quality.is_garbage_entity`，与 `_pick_subject` 同口径）→ `view`；否则才 `background`。
   同时把"numbers 非空（剔纯日期值如 `9月`/`2026年`/`Q3`）且有实体"并入 hard_fact（规则 B，86% N）。

不做：A2（硬谓词+卡片日期→hard_fact），精度 42%/50% 分不清 N 与 Q，让它进 view 即可检索，不冒充硬事实。
不动：黑名单、量化词、结构词、`_has_metric`（docstring 明令"不要顺手改"）。

### P0-B · `ingest.py`：view 入库、background 留痕

- `view` → `_ingest_fact`：`predicate="HAS_VIEW"`，`category="view"`，`unverifiable=True`，
  `extra.rule_version="v3"`（回滚标记）。**成色 = 信源基线降一档**（broker B→C，foreign_ib B+→B，social C→D）——
  定性论断的可靠性低于同源硬事实，且社媒观点落 D 正是 design §10-bis 舆情 lane 的原意；
  C/D 会被 `ask._low_grade_views` 自动收进"情绪面·不同观点"段。（决策点 ①，见 §5）
- `background` → 不再 `return` 丢弃：写入新表 `background_log(doc_id, claim, entities, source_date, source_kind, reason, ts)`
  （facts.db 内，不进 FTS/向量，仅供审计与日后回填）。`report.background` 语义不变。
- `_ingest_structure` 不足两端 → 改走 view（此前计 background 丢弃）。
- `HAS_VIEW` 不进 `_ORDER_PROGRESSION` / `VERIFIABLE_PREDICATES` / `_CONTRADICTING`，不触发 supersede、数据验证、争议标记。
- 主体归属沿用 `_pick_subject`；**多实体**改动见 P1。

### P0-C · `ask.py`：view 不做"结论"头条（约 6 行，与 xiaoqing-47 的检索层大修交叉点之一）

`ask.py:44/169` `top = active[0]` → 取第一个 `category != "view"` 的 active 事实作结论；全是 view 时才用 view 并标注。
证据链、情绪面、语义/FTS 召回、排序权重**一律不改**——view 与其他事实同池同权，只是不冒充结论。
`cli.py:123` / `deep_verify.py:105` 已限定 `hard_fact`，view 天然不进 deep-check。

### P1 · 多实体不丢（dc 的第 4 点，低成本版）

- `_ingest_fact` 把完整 `f.entities` 写进 `extra.entities`（不放大 fact 数、不动主体归属）。
- `facts_store._fts_grams` 把 `extra.entities` 一并切 gram 进 FTS（与 xiaoqing-47 的 `_fts_write` 交叉点之二）。
- `ask._rank_facts`：cid 命中 `extra.entities` 中的次要实体 → `ent_hit=0.5`（首实体仍 1.0）。
- 存量：回填脚本顺带对已有事实补 `extra.entities`（UPDATE extra，不改 id），FTS 行改写。

### P2 · 抽取层（另议，本轮不做）

9 帖有卡但 LLM 没抽出晓程：`posts_to_card.py` 的 prompt 无条数上限，是 LLM 自行挑重点。可加一条
"帖内点名的每只个股至少各出一条 finding"，但要重抽全部帖子（1.2 万帖 × LLM），成本与收益需单独评估。
5 帖无卡属 dedup 层跨群去重，母本卡应存在，不是缺陷。

### 存量回填 `scripts/backfill_background.py`

- 遍历四个卡片目录，按 v3 规则分流；**只处理"fact_id 在 facts 表不存在（含 superseded/invalidated）"的 finding**，
  存在即跳过——不 revive 已被替代的旧事实（`upsert` 合并路径会复活 superseded 行，必须绕开）。
  规则 v3 是单调放宽，凡此前入库的 hard/quant/structure 分类不变、id 不变，故"不存在即新增"等价于"原判 background 的那部分"。
- structure 类 finding 跳过（未变）；D 档写 `background_log`；critique 引擎全量 fit 一次（65s）后逐条体检。
- 幂等可续跑（fact_id 确定性）；每 2000 条 commit；单实例锁；`extra.rule_version="v3"` 可整批回滚。
- 前后动作：`scripts/_db_backup.py` WAL 热备 facts.db → 回填 → `./tkb semantic build`（增量）→ `./tkb fts status` 对账
  → `.npy` 缓存按指纹自动重建。
- 运行窗口：避开 01:00 `com.kbsync.daily`；总时长估 4–5h（见 §4），建议白天盘后跑或分 lane 两晚。

### 验收

1. 晓程 4 条 finding 入库（2 条 view + 2 条 hard_fact：非农那条含 2.3 万人、黄金股估值那条按实体进 view），
   `./tkb ask "晓程科技"` 情绪面/证据链可见，且多实体那 3 条经 P1 能被 FTS 命中。
2. 社媒帖 lane 单日增量的 background 率 60% → ≤5%；`background_log` 有行。
3. 随机抽 200 条新入 view + 200 条新入 hard_fact，LLM/人工复核：hard_fact 中 N ≥ 85%，view 中 B ≤ 15%。
4. `run_tests.py` 199 passed 不退步；新增测试：view 分流、year 门槛移除、background_log 写入、结论段跳 view、回填跳过已存在 id。
   须改 1 条既有断言：`test_review_fixes.py:74`"拟使用自有资金 2000 万元购买理财产品 → background"
   在 v3 下是 hard_fact（有金额即可证伪），该断言的前提"无时间锚"已由 source_date 承担。

---

## 4. 代价与风险

**规模估算**（各 lane 卡片数 × 每卡 findings × background 率，再按 §2.4 覆盖分桶）

| | 社媒帖 | zsxq_research | ima | 合计 |
|---|---|---|---|---|
| background 总池 | ~24 万 | ~33 万 | ~36 万 | **~94 万** |
| → hard_fact（A1+B） | ~11 万 | ~14 万 | ~18 万 | ~43 万 |
| → view（C） | ~9 万 | ~10 万 | ~11 万 | ~30 万 |
| → background_log（D） | ~1 万 | ~7 万 | ~6 万 | ~14 万 |

跨群/跨卡去重会再压掉一部分；按 **+72 万行** 估：active 111 万 → ~185 万（+65%）。

| 资源 | 现状 | 增量 | 依据 |
|---|---|---|---|
| facts 表 | 991 MB | +0.65 GB | 891 B/行 |
| FTS5 | 170 MB | +0.12 GB | 160 B/行 |
| vectors_bge.db | 4.6 GB | +3.0 GB | 4.1 KB/行 |
| .npy 矩阵缓存 | 2.28 GB | +1.5 GB | 2 KB/行，mmap 加载不占常驻内存 |
| 备份轮转（com.tkbprune.daily） | — | 同比翻倍 | 本地盘增长已是已知问题 |
| 回填耗时 | — | 分类<10 min；入库 ~64 条/s → 3–4 h；向量 ~200 条/s → ~1 h | zsxq_research 全量重跑 129 min 的实测比例；`_encode` 2000 条 9.9s |

**精度风险**：view 桶 13% 是真背景（进库但成色 D/C、不做结论，可接受）；规则 B 有 10% 定性句被标成 hard_fact/HAS_CATALYST
（成色按信源不按类别，不会抬高可靠性，只是 category 标签偏硬）。
**检索侧风险**：候选池变大，`_rank_facts` 的实体/语义权重不变，但 D 级 view 数量多，"情绪面"段可能变长——该段本就"全数提炼不设上限"，
若刷屏可在 `_low_grade_views` 加条数上限（xiaoqing-47 的地盘，届时协商）。
**冲突面**：与 RECALL_FIX_PLAN_20260825 只在 `ask.py` 结论段取值、`facts_store._fts_grams` 两点交叉，均为加法。
**运维坑**：并发护栏 `pgrep -f run_daily_extract.sh` 会把命令行里含该字面量的 zsh 进程当成实例——手动跑批时拼接字符串规避；
手动 LLM 跑批 `env -u KIMI_API_KEY`（本次抽样 kimi 已 403，走的 DeepSeek）。

---

## 5. 需要拍板的决策点

1. **view 成色**：信源基线**降一档**（推荐：社媒观点落 D，与舆情 lane 设计一致）vs 保持基线（社媒 C、研报 B）。
2. **A2 是否进 hard_fact**（"已量产/给予评级"无数字句）：推荐**不进**，随 view 入库即可检索。
3. **D 档**（无实体无数字，~14 万）：推荐只进 `background_log` 不检索；反对意见是其中 28% 仍是硬事实（多为英文宏观句）。
4. **回填范围**：全量四目录（+72 万行、4–5 h、+5.5 GB）vs 先只社媒帖 lane（+20 万行、~1.5 h）看效果再扩。推荐**先社媒帖**。
5. **P1 多实体**是否本轮一起做（改 3 处 + 存量 UPDATE extra）：推荐一起做，晓程案例 3/4 条依赖它才有排序加分。
6. P2 抽取层是否立项。

---

## 6. 实施记录（2026-08-26）

- 14:30 代码落地，217 tests 绿。14:33 全量回填开跑（社媒帖 lane 16:20 完成：findings 407,143 → 新增 hard 111,705 / view 119,628 / 留痕 9,472，耗时 106 min，约 67 条/s——比副本基准慢，因生产库上每条新硬事实要做冲突消解查询 + 质疑体检）。
- **体检发现历史重复**（只读体检脚本，见 §7）：同句多行 62,444 组 / 135,151 行（active 10%），其中同一来源文档内真重复 ~52k 组；成因是 fact_id 随主体/谓词口径演进而变、旧行不被替换。**本回填最初按 fact_id 判重，社媒 lane 新行里 4.2%（~10k 组）与旧行同文同句重复**——"v3 相对 v2.1 单调放宽"成立，但库里存量是 v1/v2 时期入的，前提在这些行上不成立。修法：判重改为 (doc_id, 归一 claim) 索引（任意状态命中即跳过、只补 entities）。
- 16:47 发现回填在 8GB 内存机器上换页（swap 5.3/6 GB、CPU 8%）：research lane 67 万 findings 对象 + 全量卡片 dict + 140 万键索引常驻。改为流式两遍读卡、blake2b 8 字节哈希键、已存在行每 500 条一事务、`synchronous=NORMAL`；副本实测 300 张 research 卡 41s。18:36 续跑 research → ima → report_lab。
- 对抗审查（独立子 agent）对 `dedup_same_claim.py` 的判定：修完 P0 再 apply。已修：P0-1/P0-2 事务内重读 + `AND sources=旧值` 乐观条件 + rowcount 校验 + 回填锁互斥 + `--deadline`；P1-1 组内 ≥2 个证券代码主体整组不合并（实测 174 组是刻意多主体拆分）；P1-2 keeper 优先级改为类别硬度第一（否则 2,272 组 v3 view 吃掉旧 hard_fact）；P1-3 `facts_merged_archive` 归档 loser 与 keeper 合并前整行；P2 版本号解析、supersedes/doubts 并集、结构分支判重、entities 并集、备份名可轮转。新增 `tests/test_dedup_same_claim.py` 5 例，全套 222 绿。**dedup 尚未 --apply，待回填结束、用户确认后执行，避开 01:00。**

- **全量回填收官（19:13）**：research lane 908s（+126,838 hard / +101,494 view / 留痕 72,934）、ima 628s（+163,979 / +115,294 / 52,491）、report_lab 28s（+160 / +221 / 147）；active 1,114,169 → 1,897,345。
- **dedup 已 --apply（20:37–20:40，用户确认后由等待脚本自动执行）**：51,824 组合并 → 删除 54,603 行，跳过 历史状态 394 组 / 多证券主体 174 组；active 1,897,345 → **1,842,742**。归档表 `facts_merged_archive`：keeper_before 51,824 + loser 54,603（可回滚）。备份 `data/backups/facts.db.bak_dedup_*`。
- **合并后体检**：同文同句真重复组 62,444 → **174**（全是刻意跳过的多主体组）；实体引用 0 悬空 / 0 已合并；support_count≠len(sources) 0；未知主体 7.78%；background_log 146,222。
- **排位对照（5 案例，before 14:24 → after_dedup 20:43）**：球硅 F 位 [5,8,12] 不变；燃机 杰瑞 [2,11,20,39] 不变、东方电气 7 不变、潍柴 20 不变；精智达/银轮/长鑫 前 5 中 4–5 条不变（精智达/长鑫各有 1 条 view 挤进第 4/5 位——xiaoqing-47 指出这是回填而非 dedup 的效应：回填后·合并前的 rank_after.md 与 rank_after_dedup.md 之间向量未变，两者对比 dedup 单独效应=**前列零变化，尾部 0~6 位位移**；位移不满足「纯删行」应有的单调性，原因是合并不是纯删行——keeper 吃掉 loser 的 sources 使 support_count 上升、evidence_level 取高、valid_at 取早、unverifiable 取 AND，全是 `_rank_facts` 打分项，变的是组代表的**分数**。该测量在 ~67% 向量覆盖率下取得，能否外推到全覆盖未知（续建又提交几批后同一事实位次可移 100+ 位）；before 侧向量全覆盖而 after 侧仅 ~67%，三份快照必须等向量建完在同一索引上重测。确定性：SIGSTOP 冻结续建后同 5 个 query 连跑两遍逐字节一致（rank_det_1/2.md）——反过来说**向量续建运行期间 `tkb ask` 不可复现**，同 query 前后答案不同是覆盖率在变，不是故障。回填行可由 `extra.rule_version=v3` 识别，fact_id 清单另落盘 `background_fix_scratch/backfill_v3_fact_ids.txt`）。view 条数 0 → 88–123。用时：语义路径 15.7s → 18.8s（库大 65%）、快路径 4.2–4.5s 不变；第一次冷启动因 .npy 缓存重建一次 107s（WAL 未 checkpoint 时指纹变动导致重建两次，第二次后稳定）。
- **`./tkb ask "晓程科技"` 验收通过**：社媒 F3/F12（海外金矿利润弹性）、F14（COMEX 5400）、F6（非农）已进入 §六 情绪面·分歧视角，标 C/D 级待验证。
- **语义向量未建完**：8GB 机器上 bge 编码实测仅 ~20 条/s（swap 7GB），1,842,742 条中已建 ~1,234k；19:13 那次 build 被我手动停掉（进度按批已提交），改为 nohup 夜间续建并定时 00:45 自动停止避开 01:00 kbsync；余下由 kbsync 的 `semantic build` 与次日手动续建完成。未建完前语义召回只覆盖 ~65%，快路径/FTS 不受影响。FTS 漂移 +3267 待 `./tkb fts build` 对账（dedup 已同步删 FTS 行）。

## 7. 复现

```bash
cd ~/trading_kb
python3 docs/background_fix_scratch/sample_bg.py /tmp/bg_sample.jsonl      # 各 lane 抽样分流 + 分桶
LLM_ORDER=deepseek env -u KIMI_API_KEY python3 docs/background_fix_scratch/tag_llm.py   # 400 条 LLM 分档(需在有 bg_sample.jsonl 的目录跑)
# 已标注结果：docs/background_fix_scratch/bg_tagged.jsonl
```

### 6.1 同一索引上的三方排位对照（2026-08-27 10:46–11:10，`background_fix_scratch/rank_threeway.sh`）

向量索引三次均为 1,843,788 条（kbsync 全程停在 ② 抽取段未动库）；变体库用 `TKB_DATA_DIR` 指向临时拷贝，向量/实体/结构库软链接共享。no_v3 = 现库剔除 779,694 条 `rule_version=v3` 回填行（≈回填前，active 1,070,338）；pre_dedup = dedup 前备份 + 回放 valid_at 修复 560,336 行（active 1,897,345）；current = 现库（active 1,850,032，含今晨 kbsync 新入 7,348）。

| 案例 / 命中前 5 序号 | no_v3（回填前） | current | pre_dedup |
|---|---|---|---|
| 球硅 | [1,8,11,14,91]（命中 7） | [1,7,10,11,13]（命中 19） | [1,7,10,11,13] |
| 杰瑞 | [6,20,35,58,60] | [6,19,41,59,60] | [6,19,41,59,60] |
| 东方电气 | [29,34,92,169,183] | [18,43,58,68,101] | [18,43,58,68,101] |
| 潍柴 | [20,108,115,179,276] | [19,126,128,214,342] | [19,125,126,214,340] |
| 精智达 / 银轮 / 长鑫 | [1,2,3,5,6] / [1,2,4,5,6] / [1,2,3,4,5] | 同左 | 同左 |
| view 条数 | 0–8 | 95–137 | 91–135 |

结论：① **v3 回填效应**（no_v3 → current）：硬事实前列位置不退步，球硅/东方电气首位反而前移（回填补进了同主题硬事实），view 进池 95–137 条但结论头条仍是硬事实；② **dedup 效应**（pre_dedup → current）：7 个案例前 5 序号全部相同或 ±2（潍柴尾部），即 ≈0；③ 本轮所有查询用时为昨夜 3–4 倍（语义 50–70s、快路径 15–17s），是 kbsync ② 段 LLM 抽取 + OCR 同时占机（swap 3GB）的系统负载，三变体同条件可比、不作性能结论。

### 6.2 重复回涨的新发现（2026-08-27 体检）
同文同句真重复组 174 → **212**（+38），48 个已删 loser 的 fact_id 又出现在 facts。溯源：今晨新入 7,348 行中 35 行与旧行 (doc, claim) 相同，**全部来自互动问答 `ir:` 文档的 14 天缺口回补**——同一问答再次入库时主体归一/规则演进使 fact_id 变化，`upsert` 只按 dedup_key/fact_id 判重就当成新事实。这是与本文 §2 同根的结构性缺口：**生产入库路径没有 (doc_id, 归一 claim) 判重**，任何再入库（回补窗口、卡片被文本回填改写后 mtime 变化、重抽取）都会把合并掉的重复慢慢造回来。建议 P1：facts_store 增 `doc_claim(doc_id, ckey, fact_id)` 表（首插维护 + 一次性回填），`Ingester.ingest_finding` 入口按它判重（命中只补 entities/来源），dedup 合并时改挂 keeper。**已实施（2026-08-27 11:20–，用户「按你的建议来」）**：`facts_store` 新表 `doc_claim(doc_id, ckey, fact_id) WITHOUT ROWID`（ckey = blake2b(归一 claim) 8 字节整数，与 backfill_background 同口径）；`upsert` 入口对 `fact.sources` 任一文档命中同句即视为同一事实——返回旧行 id、命中活跃行时并 entities、superseded 行也算存在不复活；例外：两行分挂**不同**证券代码（同句刻意拆到两只股票，dedup 也整组保留的那类）。插入路径与合并路径（新 doc 并入 sources 后）自动登记；`clean_entities._reattribute`（所有 merge_* 脚本共用）、`requalify_quant`、`dedup_same_claim` 改写/删除 fact_id 时同步 remap；`./tkb docclaim build|status` 全量对账/看悬空。`IngestReport.dup_skipped` 计数拦下的再入库。判重放在 `FactsStore.upsert` 而非 `ingest_finding`，因为 ir_qa_to_kb / announcements_to_kb / ingest_kb_cards 跨市场边都直接调 upsert。新增 `tests/test_doc_claim.py` 6 例（同文同句异 id 拦下、多证券拆分放行、superseded 不复活、entities 并集、回执计数、build/remap/dedup 改指），`test_dedup_same_claim` 改为清登记模拟存量重复，全套 228 绿。全量回填与 kbsync ② 段的 `ingest_kb_cards`（抽完 zsxq_research 会直接入库，② 段并非只写卡片）抢写锁失败一次，`_doc_claim_flush` 加锁退避重试后重跑。
- **2026-08-27 全面审核修正（docs/AUDIT_FIX_PLAN_20260827.md）**：上文"插入路径与合并路径自动登记"只登记了旧 claim（dedup_key 只看 claim[:80]，长 claim 变体绕过判重），"例外"双证券第二行在二元主键下登记不上（174 行不受保护）——已改三元主键 (doc_id, ckey, fact_id) + 合并路径登记新旧 claim + `find_doc_claim_dup` 唯一判定点；`docclaim build` 现在先清悬空；remap 一律 `UPDATE OR REPLACE`。P0-C "view 天然不进 deep-check"只对 `deep_verify_fact` 成立，`auto_verify_fresh` 入口已补 view 过滤。LLM 分流钩子已补 `view` 词表与对称闸门。
- **supersedes 悬空 83（dc 体检）**：用 `facts_merged_archive` loser→keeper 映射核对，仅 1 处是 dedup 遗留，其余 82 处引用 id 不在归档表（更早的 `_reattribute` 删旧/清理遗留）；71 行已重写（可映射改指 keeper、其余剔除），旧值留 scratchpad `supersedes_fix_undo.jsonl`。
- **structure.relations 断边 183（dc 体检，未动）**：136 条端点是伪实体（`company:美伊和平协议`、`concept:上游`），抽取质量问题；47 条指向已合并实体，是 `EntityRegistry.merge` 不回写 relations 的历史债（昨晨一次性修过 8,399 条后又新生）。治本要在 merge 入口同步改指 + 伪实体闸门覆盖 concept 前缀，待用户拍板。

### 6.3 后续两项（2026-08-27 11:50–12:00，用户「按你的建议改」）
- **垃圾端点不建边**：`ingest._ingest_structure` 在选定两端后剔除 `is_garbage_entity(e,"concept")` 命中的名字，不足两端走 view/留痕（reason `structure_garbage_end`）；`ingest_kb_cards._ingest_cross_market` 同样对 global/a_share 两端过闸。新增 `tests/test_garbage_endpoints.py` 7 例（海外市场/美伊停火/上游/公司 判垃圾、双垃圾端不建边、单垃圾端退 view、真实两端仍建边），全套 239 绿。存量 135 条孤儿边与 47 条已合并引用由 xiaoqing-dc 用 `prune_orphan_relations.py` / `fix_relation_merged_refs.py` 清掉，`EntityRegistry.merge` 已同步改指 relations。
- **OCR 回填 runner 改接力**：`~/kb_sync/sync_all.sh` 在「KB-SYNC DONE」后 nohup 拉起 `run_backfill_text.sh`（不占日更 18h 上限、不被 run_timeout 连坐、runner 自带实例锁/闸口/00:30 截止）；launchd `com.kbtextindex.daily` 20:00 定时已 bootout，plist 移到 `~/ZSXQ/kb_adapter/.backup/`。原因：20:00 定时与 01:00 日更必然撞车，闸口每天空等 11~15h（08-25 等 689min、08-26 等 940min）。今天这轮已手动拉起（pid 91328，等 ③ 段结束即开工）。运行中脚本一律临时文件 + mv 原子替换。

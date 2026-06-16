# trading_kb — A股个人投研知识系统

按 `design_final.md (v2.2)` 落地的可运行实现。复用 report_lab 的 extract+verify 护城河,
在其上补**时序事实层 + 结构关系层 + 双轨成色 + 双通道摄入**。

## 设计映射(代码 ↔ 设计文档)

| 模块 | 设计章节 | 生产可平替 |
|---|---|---|
| `report_lab_adapter` | §6 复用 | report_lab(已有) |
| `classify` 分流器 | §7.3 + 量化扩展 | LLM 复判钩子 |
| `grade` 双轨成色 | §10.1 / §19 | — |
| `web_enrich` / `announcement` 联网核实 | §8 / §19 | 公告**已实测接通**:巨潮cninfo(免费无额度,沪深全量)+orgId精确查询+18大类分类+PDF正文提取+上交所兜底+退避重试;tdx无公告/不接hibor/智能选股4000月不用于公告 |
| `facts_store` 时序事实 | §18 / §10.3 | **Graphiti** |
| `structure_store` 结构图 | §18 F6 | **LightRAG** |
| `entity_registry` 实体归一 | §17 | tdx 代码表 |
| `sentiment_lane` 舆情轻lane | §10-bis | — |
| `ask` 六段式 | §11 | — |

> 重后端(Graphiti/LightRAG/RAGFlow/MinerU)在本实现里用 SQLite 忠实实现其**语义**,
> 保证整套可离线运行与测试;上线时按设计平替为对应成熟件即可,接口不变。

## 设计立场(为何这样实现)

- **确定性核心 + LLM 钩子**:`classify`/`grade` 默认走规则核心,可复现、可测试;
  `TKB_USE_LLM=1` 启用 LLM 复判。
- **查不到 ≠ 证伪**(§3 铁律5):`grade` 对不可验证类保留信源基线 + `unverifiable`。
- **证伪不删除**(§16.1):`facts_store.contradict/supersede` 只置 `invalid_at`,可 `include_invalidated` 查回。
- **双通道隔离**(§10-bis):舆情走 `sentiment_lane`,默认 D 级、不进研报证据链,印证才升级。

## 用法

```bash
cd ~/trading_kb

# 跑测试
python -m pytest -q            # 或 python run_tests.py(无 pytest 时)

# 研报重 lane:摄入 report_lab 已有卡片
python -m trading_kb.cli ingest                 # 需先 cd src 或装包,见下

# 三层规模
python -m trading_kb.cli stats

# 六段式问答
python -m trading_kb.cli ask "绿的谐波 定点"

# 舆情轻 lane 演示
python -m trading_kb.cli sentiment-demo
```

运行 CLI 前确保 `src` 在 path:`PYTHONPATH=src python -m trading_kb.cli ...`。

## 数据

- 输入:`~/report_lab/cards/*.json`(已有 63 篇研报)
- 输出:`data/{facts,structure,entities,sentiment}.db`

## 已知边界(诚实记录,经两轮 agent 审查+模拟验证)

- **语料决定能力上限**:本地 62/63 为量化研报,硬事实(订单/产能)仅 ~5 条,产业链结构关系实测 **0 条**。`structure_store`(LightRAG 等价层)与"多源印证升级成色"在当前量化语料下基本空转——这是**语料缺口,非代码 bug**。灌入行业/公司研报后即可激活。
- **去重粒度**:事实 `object=claim[:80]`,故 dedup 近似"精确 claim 匹配",不同措辞的同一论断当前不合并(`support_count` 多为 1)。生产可换语义相似度合并。
- **结论排序**:六段式"结论"取最相关项,**暂不理解"最高/最低"等数值语义**(B3),复杂排序需 LLM 介入。
- **LLM 钩子**:`classify/grade` 默认规则核心(可复现)。`config.USE_LLM` 仅为意图标志;真实 LLM 分类器需调用方给 `run_ingest(llm_classify=...)` 注入(尚未默认接 report_lab 模型链)。
- **数据验证**:`verify_hooks` 默认安全桩(不实查);启用需 `TKB_USE_DATA_VERIFY=1` 且接好数据源(§23.1 事件驱动控额度)。
- **实体归一**:卡片 code 覆盖率约 16%,无 code 的股票落 `stock_pending:`,可后续 tdx 补齐/合并。

## 审查与修复轨迹

经两个独立 agent 把关:
1. **代码审查 agent**:查需求-实现一致性,发现 supersede 自碰撞数据丢失、状态机未接管线、市场代码路由错等;已全部修复并加回归测试。
2. **模拟实验 agent**:用真实 63 篇端到端跑,发现非 dict 卡片崩溃、并发崩溃、基金/ETF 错挂、召回坍缩、中文无空格零召回等;已修复 A1/A2/A3/A4/B1/B2/B4/C4/C5 等并补测试。

当前:**45 测试全绿**(含真实语料不变量),崩溃路径全部加固,可作内部 MVP 试用。

"""分流器:把一条 finding 判为 hard_fact / structure / quant_fact / background。

对应 design_final.md §7.3 分流判定口诀 + 量化扩展(本地语料以量化研报为主):
- hard_fact  : 带日期、可证伪的硬事实(订单/产能/中标/交付/价格/政策/财务/评级)→ Graphiti
- structure  : 稳定的 typed 关系(上游/供应/竞争/属于某环节)→ LightRAG
- quant_fact : 量化因子/策略表现声明(本地真实语料主力)→ facts_store(factor 类)
- background : 定性/预测/背景 → 留原文,不入图

默认走确定性规则核心(可复现);TKB_USE_LLM=1 时可叠加 LLM 复判(预留钩子)。

── 2026-08-01 规则重估(v2)：补三个结构性缺口 ──────────────────────────────
起因：实测 1500 张研报卡片 49148 条 findings，**70.8% 落进 background 兜底**，而其中
真正该丢的模板文字只占 0.5%，带硬数据的(百分比/货币/numbers)占 71.3%。逐条抽样确认
是【规则误杀】而非 LLM 抽废话——野村税前利润915亿日元同比-33%、Dato-DXd 临床 mPFS
7.2 vs 4.1、China Tower 评级O目标价HK$10.75 这类都被丢了。三个缺口：
  ① 词典原本全中文，而 background 里 55.1% 是英文(外资投行研报占语料极大比重)
  ② 谓词只覆盖产业事件(订单/产能/价格/政策)，缺财务、评级、业绩指引
  ③ 带硬数据但不含上述特定谓词的陈述，一路掉进 else 兜底
修法：补英文谓词、补财务/评级词典、加"具体数值+可证伪标记"兜底路径，并新增黑名单把
免责声明/评级分布披露这类合规模板【明确丢弃】(此前它们和真事实混在一起)。
实测 background 70.9%→42.1%(研报)、74.0%→58.4%(社媒)。
精度护栏：数值兜底只认百分比/货币金额，**不用 f.numbers**(它常来自日期，会把
"铜的COMEX库存大幅增加"这类纯定性句捞进来)；structure 判定排在数值路径之前，
否则带数字的产业链关系会被误收成 hard_fact。

── 2026-08-06 精度修补(v2.1)：对抗性审查后五处收口 ─────────────────────────
① 量化词 ASCII 整词化(裸 "ic" 命中 price/historic,实测 quant_fact 88% 误判)+ 弱词双共现;
② 评级词典只认自足强词/复合评级句式,股东增减持单列 HAS_INSIDER_TRADE;
③ 裸年份加边界(2000万/2050亿/证券代码不再当年份);
④ 黑名单收窄(past performance 只认免责句式,删"评级的12个月");
⑤ relation_for 补英文映射(competitor 不再落成 BELONGS_TO_SEGMENT)。
存量数据由 scripts/requalify_quant.py 按新规则重判迁移。
"""
from __future__ import annotations

import re
from typing import Optional

from .models import Finding, Category, Relation

# ── 关键词词典(规则核心)─────────────────────────────────────────────────
# 硬事实:有公告/产业事件可对的(§19 可验证类 predicate)
_HARD_FACT_KW = {
    "HAS_CONFIRMED_ORDER": ["中标", "签订", "合同订单", "正式订单", "公告订单", "获订单", "斩获"],
    "HAS_ORDER_INTENT": ["定点", "客户意向", "送样", "预计导入", "进入供应链", "通过认证"],
    "HAS_ORDER_RUMOR": ["传闻", "小作文", "或将", "据传", "市场传"],
    "HAS_CAPACITY": ["产能", "扩产", "投产", "在建", "达产", "产线"],
    "HAS_PRICE_SIGNAL": ["涨价", "提价", "降价", "报价", "价格上调", "价格下调"],
    "HAS_DELIVERY_VALIDATION": ["批量供货", "放量", "出货", "交付", "量产"],
    "HAS_POLICY_SUPPORT": ["政策", "补贴", "规划", "标准发布", "文件"],
}
# 结构关系:产业链 typed 边
_STRUCTURE_KW = {
    "UPSTREAM_OF": ["上游", "原材料", "核心部件供应"],
    "SUPPLIES": ["供应", "供货", "配套", "为.*提供"],
    "COMPETES_WITH": ["竞争", "对手", "替代"],
    "BELONGS_TO_SEGMENT": ["属于", "环节", "细分领域", "赛道"],
}
# 量化事实:因子/策略表现。
# v2.1(2026-08-06 修):原列表把 "ic" 当裸子串匹配,英文 price/historic/specific 等常词
# 全部命中,叠加任意数字即误判 quant_fact——实测随机 2000 条抽样 88% 误判、固化 22.7 万行。
# 修法:① ASCII 词必须整词(\b 边界);② 强词(因子/回测/IC 等)单独命中即算,
# 弱词(年化/超额/策略/组合)在宏观、资产配置语境高频同形,须两个及以上不同弱词共现才算量化语境。
_QUANT_STRONG_RE = re.compile(
    r"(因子|回测|夏普|信息比|胜率|回撤|多空|选股|"
    r"\bic\b|\bicir\b|\brankic\b|\bsharpe\b|\bbacktest\b|\balpha\b)", re.I)
_QUANT_WEAK_RE = re.compile(r"(年化|超额|策略|组合)")


def _match_quant(text: str) -> bool:
    """量化语境判定:强词单命中,或 >=2 个不同弱词共现(见上方 v2.1 说明)。"""
    if _QUANT_STRONG_RE.search(text):
        return True
    return len(set(_QUANT_WEAK_RE.findall(text))) >= 2
# 时间标记(硬事实需可证伪 → 通常带时间/数字)
_DATE_RE = re.compile(r"(20\d{2}[-/年]\d{1,2}|20\d{2}\s*年|\d{4}Q[1-4]|[一二三四]季度)")

# ── v2 新增词典(2026-08-01)──────────────────────────────────────────────
# 券商模板文字:免责声明/分析师认证/评级体系定义/评级分布披露。
# 必须【明确丢弃】而非落兜底——它们本身不是事实,且在英文研报里高频出现。
# v2.1 收窄四处:① "past performance" 裸词会吃掉真实量化表现陈述("the factor's past
# performance shows IC 0.05"),改为只认免责句式(past performance 后跟 not/no/不);
# ② "评级的12个月" 会吃掉"维持买入评级的12个月目标价为X元"这类真实目标价句,而它要拦的
# 评级体系定义句("买入:评级的12个月总回报预期高于…")已被"总回报预期"覆盖,故删除;
# ③ "disclosure" 裸词会吃掉从外资行 Company Disclosures 附表抽出的**真实评级/目标价**
# (evidence 常写"Company Disclosures表中显示:…rating Buy, Price NT$2,820"),
# 收窄为合规节标题句式(required/important disclosures、disclosure statement/section);
# ④ "disclaimer" 排除审计意见 "disclaimer of opinion"(无法表示意见,高价值利空硬事实)。
_BOILERPLATE = re.compile(
    r"((required|important|global|price target|risk) disclosures?|"
    r"disclosures? (statement|section|appendix|continued)|"
    r"disclaimer(?!s? of opinion)|analyst certification|provided to private banking|"
    r"is not a research report|for institutional investors only|"
    r"past performance.{0,30}(not|no |不)|"
    # consensus rating distribution 是"该股卖方一致评级分布"(真实市场数据),非合规披露,放行
    r"(?<!consensus )ratings? distribution|of covered companies|investment banking clients|"
    r"免责|本报告仅供|总回报预期|不构成投资建议|版权(所有|归属)|"
    r"投资评级\d|评级股票占覆盖|占覆盖总数|投资银行客户)", re.I)

# 英文硬事实谓词,与上面中文 _HARD_FACT_KW 的 predicate 分类一一对齐
_HARD_FACT_KW_EN = {
    "HAS_CONFIRMED_ORDER": r"(awarded|won (the )?(contract|bid)|signed (an? )?(agreement|contract)|secured (an? )?order|book(ed)? order)",
    "HAS_ORDER_INTENT": r"(design win|qualified|sampling|entered .{0,20}supply chain|certification)",
    "HAS_CAPACITY": r"(capacity|expansion|production line|new fab|ramp(-| )?up|utilization rate)",
    "HAS_PRICE_SIGNAL": r"(price (increase|hike|cut|decline)|\basps?\b|pricing power|raise(d)? price)",
    "HAS_DELIVERY_VALIDATION": r"(mass production|volume (production|shipment)|shipment|deliver(y|ies)|began shipping)",
    "HAS_POLICY_SUPPORT": r"(subsid(y|ies)|tariff|regulation|policy support|export control)",
}
# 财务指标(中英):营收/利润/毛利/指引/资本开支/在手订单
_FINANCIAL_KW = re.compile(
    r"(营收|营业收入|净利|归母|毛利率|净利率|净利润|每股收益|资本开支|指引|出货量|销量|"
    r"revenue|earnings|net income|gross margin|operating margin|ebitda|eps|"
    r"guidance|capex|opex|free cash flow|backlog|order book|yoy|qoq)", re.I)
# 评级/目标价(中英)。
# v2.1 收紧:原词典的裸词"上调/下调/增持/减持/买入/卖出/neutral"与股东增减持、指引调整、
# 资金流向、宏观预测修正大量同形(抽样 12 条误标约半数:AA+主权评级/CPI预测上调/FMS
# underweight/股东减持全进了 HAS_RATING)。改为只认自足强词:评级/目标价/首次覆盖/英文
# 评级动作,外加"维持|重申|给予|首予|上调至|下调至 + 评级档位词"的复合句式(覆盖不带
# "评级"二字的真实评级表述,如"维持买入""下调至中性")。
_RATING_KW = re.compile(
    r"(评级|目标价|首次覆盖|"
    r"(维持|重申|给予|首予|上调至|下调至)(买入|增持|中性|持有|减持|卖出|推荐|优于大市|跑赢行业)|"
    r"price target|target price|initiat\w{0,4} coverage|"
    r"overweight|underweight|outperform|underperform|market.?perform|"
    r"upgrade[ds]?\b|downgrade[ds]?\b)", re.I)
# 股东/董监高增减持(v2.1 加):与"增持/减持评级"同形但语义完全不同,单列 predicate
# 让下游能区分"股东行为"与"券商评级"。主体词与动作词最多间隔 12 字(覆盖"控股股东
# 拟通过大宗交易减持"式插入语)。
_INSIDER_RE = re.compile(
    r"(股东|董事|监事|高管|董监高|实际控制人|实控人|员工持股|回购).{0,12}(增持|减持|回购)"
    r"|(增持|减持)(计划|股份|公司股份|比例)")
# 结构关系英文补充(与 _STRUCTURE_KW 互补)
_STRUCTURE_KW_EN = re.compile(
    r"(upstream|downstream|supplier|supplies to|competitor|compete with|"
    r"market share|value chain|substitute for)", re.I)
# 预测/展望标记(2026-08-01 加)：区分【尚未发生】与【已发生】。
# 原规则靠"必须命中订单/中标/产能等已发生事件谓词"天然挡住预测；v2 用"数值+年份"
# 替代谓词后这道边界失效,实测 hard_fact 里混入约 28% 预测。
# 处理方式不是丢弃——目标价/盈利预测/CAGR 展望本身是高价值投研信息——而是给它
# 独立 predicate(HAS_FORECAST),让下游能按"事实 vs 预测"区分检索。
# ⚠ 刻意不收"预期/超预期/低于预期"裸词：那多是对【已公布数据】的评价
#   (如"出口同比增长21.8%,远超市场预期"),收了会把已发生事实误判成预测。
_FORECAST_KW = re.compile(
    r"(预计|预期将|预测|有望|或将|料将|展望|未来几年|"
    r"expected to|is forecast|forecast to|estimat\w* to|projected to|"
    r"outlook for|cagr|20(2[7-9]|[3-9]\d)\s*e\b)", re.I)
# 具体数值:只认百分比与货币金额(见模块 docstring 的精度护栏说明)。
# v2.1 补"元":A股目标价/派息几乎都写"72元/每10股派3元",原表只认 万/亿 级单位,
# "目标价72元"没有数值锚会整句掉 background。中文数字(三元锂)前无阿拉伯数字,不受影响。
_PCT_RE = re.compile(r"\d+\.?\d*\s*%")
_MONEY_RE = re.compile(r"(\d+\.?\d*\s*(亿|万|百万|十亿|元|bn|mn|billion|million)|[\$￥€£]\s*\d)", re.I)
# 可证伪时间标记:在 _DATE_RE 基础上补英文财季/财年格式(2026 / FY26 / 1Q26 / Q1 2026)。
# v2.1 修:裸年份分支原为 `20\d{2}`,会把"2000万/2050亿"的金额头四位、"002088"式证券代码
# 里的数字段当年份,"数值+年份"护栏被架空。改为:前不接数字/小数点、后不接数字与
# 万/亿/千/百/元/%(金额单位),且限定 2000-2039 合理年份区间。
_YEAR_RE = re.compile(r"(20\d{2}[-/年]\d{1,2}|20\d{2}\s*年|\d{4}Q[1-4]|[一二三四]季度|"
                      r"(?<![\d.])20[0-3]\d(?![\d万亿千百元%])|"
                      r"FY\d{2,4}|[1-4]Q\d{2}|Q[1-4]\s*20\d{2})", re.I)


def classify_finding(f: Finding, llm=None) -> Category:
    """对单条 finding 分类。llm 为可选复判钩子(签名 llm(finding)->Category)。

    判定顺序即优先级,不可随意调换(见模块 docstring 的精度护栏):
      0 黑名单 → 1 量化 → 2 硬谓词(中/英) → 3 结构关系 → 4 财务/评级/数值 → 5 兜底
    结构关系必须排在第 4 组数值路径之前,否则带数字的产业链关系会被误收成 hard_fact。
    """
    raw = f"{f.claim} {f.evidence}"
    text = raw.lower()

    # 0) 券商模板文字(免责/评级体系定义/评级分布)→ 直接丢,不进兜底。
    # 只查 claim 不查 evidence(v2.1):evidence 常引用出处("Company Disclosures表中显示…")
    # 或带礼节性免责尾巴,按 evidence 判会把真实评级/答复整条误杀——claim 才是事实本体。
    if _BOILERPLATE.search(f.claim or ""):
        cat: Category = "background"
    # 1) 量化因子事实优先识别(本地语料主力,避免被误判 background)
    elif _match_quant(text) and _has_metric(f):
        cat = "quant_fact"
    # 2) 硬事实:命中硬事实词(中文原规则 / 英文新增) + (带时间或带数字,体现可证伪)
    elif _match_hard_predicate(text) and (_DATE_RE.search(text) or f.numbers):
        cat = "hard_fact"
    elif _match_hard_predicate_en(text) and (_YEAR_RE.search(raw) or f.numbers
                                             or _has_hard_number(raw)):
        cat = "hard_fact"
    # 3) 结构关系(中文原规则 + 英文补充)
    elif _match_structure(text) or _STRUCTURE_KW_EN.search(text):
        cat = "structure"
    # 4) 财务指标 / 评级目标价 / 纯数值,均要求带具体数值才算可证伪
    elif _FINANCIAL_KW.search(raw) and (_has_hard_number(raw) or f.numbers):
        cat = "hard_fact"
    elif ((_RATING_KW.search(raw) or _INSIDER_RE.search(raw))
          and (_has_hard_number(raw) or f.numbers)):
        cat = "hard_fact"
    elif _has_hard_number(raw) and _YEAR_RE.search(raw):
        cat = "hard_fact"
    # 5) 其余为背景/定性
    else:
        cat = "background"

    # LLM 复判(预留):仅在开启且规则给出低置信时介入
    if llm is not None:
        override = llm(f)
        if override in ("hard_fact", "structure", "quant_fact", "background"):
            cat = override
    return cat


def predicate_for(f: Finding) -> str:
    """为 hard_fact 选 predicate;命中多个取最强信源(确认>意向>传闻)。

    实际判定顺序:中文谓词 → 英文谓词 → 股东增减持 → 评级 → 预测 → 财务 → 兜底
    (v2.1 修正 docstring 与代码不一致:评级排在财务之前是刻意的,理由见下方行内注释)。
    新 predicate(HAS_FINANCIAL_METRIC / HAS_RATING / HAS_INSIDER_TRADE)不在
    _ORDER_PROGRESSION 与 VERIFIABLE_PREDICATES 白名单内,故不参与 supersede、
    不触发数据对抗验证,成色仍按 source_kind 基线走,对既有口径无副作用。
    """
    raw = f"{f.claim} {f.evidence}"
    text = raw.lower()
    # 强度优先级
    order = ["HAS_CONFIRMED_ORDER", "HAS_DELIVERY_VALIDATION", "HAS_CAPACITY",
             "HAS_PRICE_SIGNAL", "HAS_POLICY_SUPPORT", "HAS_ORDER_INTENT", "HAS_ORDER_RUMOR"]
    for pred in order:
        if _hit_any(text, _HARD_FACT_KW[pred]):
            return pred
    for pred in order:                                  # 英文谓词,同一套强度优先级
        pat = _HARD_FACT_KW_EN.get(pred)
        if pat and re.search(pat, text):
            return pred
    # 股东/董监高增减持:与"增持/减持评级"同形,先于评级判定(公告 lane 同名 predicate)
    if _INSIDER_RE.search(raw):
        return "HAS_INSIDER_TRADE"
    # 评级动作(上调/下调/给予评级)本身是【已发生】事件,排在预测判定之前——
    # "目标价上调至X,因某某有望推动Y"的主语是评级动作,不是那句展望。
    if _RATING_KW.search(raw):
        return "HAS_RATING"
    # 尚未发生的展望 → 独立 predicate,不冒充可证伪硬事实
    if _FORECAST_KW.search(raw):
        return "HAS_FORECAST"
    if _FINANCIAL_KW.search(raw):
        return "HAS_FINANCIAL_METRIC"
    return "HAS_CATALYST"


# 英文结构词 → typed 关系映射(v2.1 加)。此前英文词只在 classify_finding 里判"是结构",
# relation_for 无英文词典,所有英文边都落默认 BELONGS_TO_SEGMENT——competitor 被记成
# "属于某环节",边类型错。顺序按方向性强弱:供应/竞争先于泛化的份额/链路。
_STRUCTURE_KW_EN_MAP = [
    ("SUPPLIES", re.compile(r"(supplier|supplies to)", re.I)),
    ("COMPETES_WITH", re.compile(r"(competitor|compete with|substitute for)", re.I)),
    ("UPSTREAM_OF", re.compile(r"(upstream|downstream)", re.I)),
    ("BELONGS_TO_SEGMENT", re.compile(r"(market share|value chain)", re.I)),
]


def relation_for(f: Finding) -> Optional[str]:
    """为 structure finding 选 typed 关系类型(中文词典优先,英文映射兜底)。"""
    text = f"{f.claim} {f.evidence}".lower()
    for rel, kws in _STRUCTURE_KW.items():
        for kw in kws:
            if re.search(kw, text):
                return rel
    for rel, pat in _STRUCTURE_KW_EN_MAP:
        if pat.search(text):
            return rel
    return None


# ── 辅助 ──────────────────────────────────────────────────────────────────
def _hit_any(text: str, kws: list[str]) -> bool:
    return any(re.search(re.escape(kw) if "." not in kw else kw, text) for kw in kws)


def _match_hard_predicate(text: str) -> bool:
    return any(_hit_any(text, kws) for kws in _HARD_FACT_KW.values())


def _match_hard_predicate_en(text: str) -> bool:
    """英文硬事实谓词命中(text 须为小写)。"""
    return any(re.search(pat, text) for pat in _HARD_FACT_KW_EN.values())


def _has_hard_number(raw: str) -> bool:
    """是否含【具体数值】——只认百分比与货币金额。

    刻意不接受 f.numbers 作为依据:该字段常由日期填充,若用它兜底,
    "铜的COMEX库存大幅增加,LME库存下降"这类纯定性句会被误收为硬事实。
    """
    return bool(_PCT_RE.search(raw) or _MONEY_RE.search(raw))


def _match_structure(text: str) -> bool:
    for kws in _STRUCTURE_KW.values():
        for kw in kws:
            if re.search(kw, text):
                return True
    return False


def _has_metric(f: Finding) -> bool:
    """是否带可量化指标(数字字段非空,或文本含百分比/比率)。"""
    if f.numbers:
        return True
    return bool(re.search(r"\d+\.?\d*%|\d+\.?\d*", f.claim + f.evidence))

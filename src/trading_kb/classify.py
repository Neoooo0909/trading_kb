"""分流器:把一条 finding 判为 hard_fact / structure / quant_fact / background。

对应 design_final.md §7.3 分流判定口诀 + 量化扩展(本地语料以量化研报为主):
- hard_fact  : 带日期、可证伪的硬事实(订单/产能/中标/交付/价格/政策)→ Graphiti
- structure  : 稳定的 typed 关系(上游/供应/竞争/属于某环节)→ LightRAG
- quant_fact : 量化因子/策略表现声明(本地真实语料主力)→ facts_store(factor 类)
- background : 定性/预测/背景 → 留原文,不入图

默认走确定性规则核心(可复现);TKB_USE_LLM=1 时可叠加 LLM 复判(预留钩子)。
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
# 量化事实:因子/策略表现
_QUANT_KW = ["因子", "回测", "ic", "icir", "夏普", "年化", "超额", "多空", "选股",
             "策略", "组合", "信息比", "胜率", "回撤", "rankic"]
# 时间标记(硬事实需可证伪 → 通常带时间/数字)
_DATE_RE = re.compile(r"(20\d{2}[-/年]\d{1,2}|20\d{2}\s*年|\d{4}Q[1-4]|[一二三四]季度)")


def classify_finding(f: Finding, llm=None) -> Category:
    """对单条 finding 分类。llm 为可选复判钩子(签名 llm(finding)->Category)。"""
    text = f"{f.claim} {f.evidence}".lower()

    # 1) 量化因子事实优先识别(本地语料主力,避免被误判 background)
    if _hit_any(text, _QUANT_KW) and _has_metric(f):
        cat: Category = "quant_fact"
    # 2) 硬事实:命中硬事实词 + (带时间或带数字,体现可证伪)
    elif _match_hard_predicate(text) and (_DATE_RE.search(text) or f.numbers):
        cat = "hard_fact"
    # 3) 结构关系
    elif _match_structure(text):
        cat = "structure"
    # 4) 其余为背景/定性
    else:
        cat = "background"

    # LLM 复判(预留):仅在开启且规则给出低置信时介入
    if llm is not None:
        override = llm(f)
        if override in ("hard_fact", "structure", "quant_fact", "background"):
            cat = override
    return cat


def predicate_for(f: Finding) -> str:
    """为 hard_fact 选 predicate;命中多个取最强信源(确认>意向>传闻)。"""
    text = f"{f.claim} {f.evidence}".lower()
    # 强度优先级
    order = ["HAS_CONFIRMED_ORDER", "HAS_DELIVERY_VALIDATION", "HAS_CAPACITY",
             "HAS_PRICE_SIGNAL", "HAS_POLICY_SUPPORT", "HAS_ORDER_INTENT", "HAS_ORDER_RUMOR"]
    for pred in order:
        if _hit_any(text, _HARD_FACT_KW[pred]):
            return pred
    return "HAS_CATALYST"


def relation_for(f: Finding) -> Optional[str]:
    """为 structure finding 选 typed 关系类型。"""
    text = f"{f.claim} {f.evidence}".lower()
    for rel, kws in _STRUCTURE_KW.items():
        for kw in kws:
            if re.search(kw, text):
                return rel
    return None


# ── 辅助 ──────────────────────────────────────────────────────────────────
def _hit_any(text: str, kws: list[str]) -> bool:
    return any(re.search(re.escape(kw) if "." not in kw else kw, text) for kw in kws)


def _match_hard_predicate(text: str) -> bool:
    return any(_hit_any(text, kws) for kws in _HARD_FACT_KW.values())


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

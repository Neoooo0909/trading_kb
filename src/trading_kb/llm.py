"""LLM 适配层：复用 report_lab 的 Kimi→DeepSeek→Sonnet 降级链处理语料。

策略（用户指定）：Kimi 优先 → Kimi 额度用尽(连续429自动判死)降 DeepSeek → 兜底 claude CLI Sonnet。
key 在 ~/.config/{kimi,deepseek}/api_key（report_lab 已配，本模块直接复用其 chat()，不重复造轮子）。

默认不参与 ingest（规则核心保证可复现/可测试）；需要时把 make_llm_classify() 注入
run_ingest(llm_classify=...) 即让分流走 LLM 复判。其他语料处理（抽取/摘要）调 complete()。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from .models import Finding

_RL_SCRIPTS = Path.home() / "report_lab" / "scripts"
_CATEGORIES = ("hard_fact", "structure", "quant_fact", "background")


def _chat():
    """懒加载 report_lab 的降级链 chat()（Kimi→DeepSeek→Sonnet）。"""
    if str(_RL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_RL_SCRIPTS))
    from common import chat
    return chat


def available() -> bool:
    """降级链是否可用（report_lab/common 可导入）。"""
    try:
        _chat()
        return True
    except Exception:
        return False


def complete(prompt: str, max_tokens: int = 2048, tier: str = "extract") -> Optional[str]:
    """走降级链取一段补全；全链失败返回 None。tier=extract 从最便宜(Kimi)起，answer 从 Sonnet 起。"""
    try:
        return _chat()(prompt, max_tokens=max_tokens, tier=tier)
    except Exception:
        return None


_CLASSIFY_PROMPT = """你是A股投研信息分类器。判断下面这条信息属于哪一类，只回一个英文词，不要解释：
- hard_fact: 可证伪的硬事实（订单/中标/产能/定点/价格/政策，通常带主体+数字或日期）
- quant_fact: 量化因子/回测表现（IC/夏普/年化/多空收益等）
- structure: 产业链/上下游/归属关系（A是B的供应商、A属于B板块）
- background: 定性背景/观点/情绪，无可验证硬信息

信息：{claim}
类别："""


def make_llm_classify():
    """返回可注入 run_ingest(llm_classify=) 的分类钩子：llm(finding)->Category|None。

    LLM 不可用或答非四类 → 返回 None，调用方自动回退规则核心（见 classify_finding）。
    """
    def _classify(f: Finding) -> Optional[str]:
        r = complete(_CLASSIFY_PROMPT.format(claim=f.claim[:300]), max_tokens=12)
        if not r:
            return None
        r = r.strip().lower()
        for c in _CATEGORIES:
            if c in r:
                return c
        return None
    return _classify


_STANCE_PROMPT = """判断下面这条投研碎片对相关标的的立场，只回一个英文词，不要解释：
bullish（看多/利好）/ bearish（看空/利空）/ neutral（中性/无明显倾向）

碎片：{text}
立场："""


def make_llm_stance():
    """返回可注入 sentiment_lane.ingest_fragment(llm=) 的立场钩子：llm(text)->stance。

    用 LLM 判聊天/短评碎片的多空立场，替代规则关键词。失败回退 neutral。
    """
    def _stance(text: str) -> str:
        r = complete(_STANCE_PROMPT.format(text=text[:300]), max_tokens=8)
        r = (r or "").strip().lower()
        for s in ("bullish", "bearish", "neutral"):
            if s in r:
                return s
        return "neutral"
    return _stance


_SYNTH_PROMPT = """你是A股投研助手。基于下面的"检索材料包"(六段式骨架，已含成色标签/质疑/出处)，
用自然语言综合回答用户的问题。要求：
- 只用材料里的信息，绝不编造材料中没有的数字或事实；
- 保留成色标签(A/B/C/D)与"待验证/质疑"提示，低成色结论要明确提示别当买点；
- 简洁、有条理，先给结论再给依据。

问题：{query}

检索材料包：
{material}

综合回答："""


def synthesize_answer(query: str, material: str) -> Optional[str]:
    """C：用 Sonnet(tier=answer) 把六段式材料包合成自然语言回答。失败返回 None。"""
    return complete(_SYNTH_PROMPT.format(query=query, material=material[:8000]),
                    max_tokens=2048, tier="answer")

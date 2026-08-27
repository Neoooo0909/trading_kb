"""LLM 适配层：复用 report_lab 的 Kimi→DeepSeek→Sonnet 降级链处理语料。

策略（用户指定）：Kimi 优先 → Kimi 额度用尽(连续429自动判死)降 DeepSeek → 兜底 claude CLI Sonnet。
key 在 ~/.config/{kimi,deepseek}/api_key（report_lab 已配，本模块直接复用其 chat()，不重复造轮子）。

默认不参与 ingest（规则核心保证可复现/可测试）；需要时把 make_llm_classify() 注入
run_ingest(llm_classify=...) 即让分流走 LLM 复判。其他语料处理（抽取/摘要）调 complete()。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

from . import config
from .models import Finding

_RL_SCRIPTS = Path.home() / "report_lab" / "scripts"
_CATEGORIES = ("hard_fact", "structure", "quant_fact", "view", "background")

_chat_fn = None


def _chat():
    """按绝对路径加载 report_lab 的降级链 chat()（Kimi→DeepSeek→Sonnet）。

    不再 `sys.path.insert(0)` + `import common`:"common" 是高碰撞模块名,抢占
    sys.path[0] 会让任何路径上的同名文件被误导入(report_lab 链路的历史事故
    多发生在这条注入链上)。改 spec_from_file_location 精确加载该文件;
    其目录以 append 补进 sys.path 尾部,仅供 common 自身的兄弟导入使用。
    """
    global _chat_fn
    if _chat_fn is not None:
        return _chat_fn
    src = _RL_SCRIPTS / "common.py"
    if not src.exists():
        raise FileNotFoundError(f"report_lab 降级链不可用:{src} 不存在")
    if str(_RL_SCRIPTS) not in sys.path:
        sys.path.append(str(_RL_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_tkb_rl_common", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_tkb_rl_common"] = mod
    spec.loader.exec_module(mod)
    _chat_fn = mod.chat
    return _chat_fn


def available() -> bool:
    """降级链是否可用（report_lab/common 可导入）。"""
    try:
        _chat()
        return True
    except Exception:
        return False


def complete(prompt: str, max_tokens: int = 2048, tier: str = "extract") -> Optional[str]:
    """走降级链取一段补全；全链失败返回 None(带 stderr 告警,§2.2 失败必须出声)。

    tier=extract 从最便宜(Kimi)起，answer 从 Sonnet 起。
    None 对调用方意味着"额度耗尽/断网/链路全死"与"模型没答"的合并降级——
    告警让链路故障不再与正常回退混为一谈(report_lab LLMUnavailable 同款教训)。
    """
    try:
        return _chat()(prompt, max_tokens=max_tokens, tier=tier)
    except Exception as e:
        print(f"[llm] 降级链调用失败({type(e).__name__}: {e}),返回 None", file=sys.stderr)
        return None


_CLASSIFY_PROMPT = """你是A股投研信息分类器。判断下面这条信息属于哪一类，只回一个英文词，不要解释：
- hard_fact: 可证伪的硬事实（订单/中标/产能/定点/价格/政策，通常带主体+数字或日期）
- quant_fact: 量化因子/回测表现（IC/夏普/年化/多空收益等）
- structure: 产业链/上下游/归属关系（A是B的供应商、A属于B板块）
- view: 有明确主体（某公司/某股票/某产品）的定性判断或观点，无可证伪的数字或事件
- background: 无明确主体的宏观/情绪/套话背景

信息：{claim}
类别："""


def make_llm_classify():
    """返回可注入 run_ingest(llm_classify=) 的分类钩子：llm(finding)->Category|None。

    LLM 不可用或答非五类(含 view,2026-08-27 补) → 返回 None，调用方回退规则核心。
    采纳规则在 classify._apply_llm_override:只在规则判低置信档(view/background)时介入,
    且 view/background 之间的改判须与"有无真实主体"一致——LLM 实际保留的是
    "把规则漏掉的硬事实/结构/量化捞回来"的能力。
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
- 保留成色标签(A/B/C/D)与"待验证/质疑"提示；但成色(可靠性)与时效(对当下决策相关性)是两个维度，分开判断、不唯成色论：老的高成色若口径已被时间淘汰(过时的折旧/出货/业绩口径)决策权重要下调；新的低成色(最近的合作/订单/边际变化)若对当下定价有实质影响，应作"当前主导变量/最新边际"积极纳入并标注"时效价值高·可靠性待交叉验证"，建议交叉核验而非因C级就一律别当买点；只压"低成色且已被反证或无独立佐证"的孤证；
- 材料含【情绪面·不同观点(C级/低成色)】段时：务必主动提炼其中的 C 级观点/情绪信号(多为社媒研究、媒体、专家纪要的前瞻判断与分歧视角)，作为"短期情绪面/分歧视角"融入结论与交易含义、逐条标注"(C级·待验证)"——情绪与分歧本身影响短期价格、也提供对立视角，别因成色低就整体略过或一笔带过；有多少相关就提炼多少，仅"低成色且已被反证或无独立佐证"的孤证不作独立买点；
- 若材料含【🌡 环境重估】段：这是实时量价/估值推出的"当前定价框架"。必须先认定该框架(成长PE/困境反转/主题beta/价值低估)，据此给基本面重新加权——框架为 beta/主题/困境反转、且标注"基本面权重=低"时，个股快变量基本面(订单/交付/减值等)对当前股价解释力弱，勿把它们当即时买卖点；慢变量(护城河/客户结构)不受此影响。让结论锚在当前坐标系，而非只复述历史基本面。
- 简洁、有条理，先给结论再给依据。

问题：{query}

检索材料包：
{material}

综合回答："""


def synthesize_answer(query: str, material: str) -> Optional[str]:
    """C：用 Sonnet(tier=answer) 把六段式材料包合成自然语言回答。失败返回 None。"""
    # 材料窗口(2026-08-25):8000 字只够证据链前 ~70 条,候选池 500+ 时后面的相关事实对 LLM
    # 等于不存在;上限收到 config.SYNTH_MATERIAL_CHARS(默认 24000,环境变量可调)。
    return complete(_SYNTH_PROMPT.format(query=query,
                                         material=material[:config.SYNTH_MATERIAL_CHARS]),
                    max_tokens=2048, tier="answer")

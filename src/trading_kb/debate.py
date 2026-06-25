"""真多轮对抗辩论(P2，借鉴 TradingAgents 精髓:多空+风控辩论，非单次综合)。

精髓在"真对抗":空头能看到多头论点并逐条反驳，风控看完整辩论按成色裁决——
不是各写一段拼接。基于 ask 的检索材料(带成色 A/B/C/D)，用 llm.complete 降级链驱动。
complete 可注入(测试用 mock，验证"空头确实看到多头、风控确实看到双方")。
"""
from __future__ import annotations

from . import llm as _llm

_BULL = """你是多头研究员。基于下面带成色标签(A/B/C/D)的检索材料，列出 3-5 条最有力的看多论点，
每条注明所依赖证据的成色。只用材料中的信息，不编造数字或事实。

问题：{query}
检索材料：
{material}

看多论点："""

_BEAR = """你是空头研究员。下面是多头论点和原始材料(带成色)。请：
1) 逐条反驳多头论点——指出其证据成色低 / 已被财报证伪 / 逻辑跳跃；
2) 补充 2-3 条独立看空论点。
只用材料中的信息，不编造。

问题：{query}
检索材料：
{material}

多头论点：
{bull}

空头反驳与独立论点："""

_JUDGE = """你是风控裁决官。基于完整的多空辩论和原始材料(带成色)，给出平衡结论：
- 哪些多头论点成色硬、站得住；哪些被空头有效反驳；
- 净判断：方向 + 关键风险 + 该跟踪的验证点；
- 低成色 / 未兑现的预期不得作为独立买点。

问题：{query}
检索材料：
{material}

多头：
{bull}

空头：
{bear}

风控裁决："""


def debate(query: str, engine, complete=None) -> dict:
    """多空对抗 + 风控裁决。返回 {material, bull, bear, verdict}。

    engine    AskEngine(取带成色的检索材料)。
    complete  LLM 补全函数(prompt, max_tokens, tier)->str|None；默认降级链，可注入 mock。
    """
    complete = complete or _llm.complete
    material = engine.ask(query).to_six_section()
    m = material[:6000]
    bull = complete(_BULL.format(query=query, material=m), max_tokens=1200,
                    tier="extract") or "(多头无输出)"
    bear = complete(_BEAR.format(query=query, material=m, bull=bull), max_tokens=1200,
                    tier="answer") or "(空头无输出)"
    verdict = complete(_JUDGE.format(query=query, material=material[:5000], bull=bull, bear=bear),
                       max_tokens=1500, tier="answer") or "(裁决无输出)"
    return {"material": material, "bull": bull, "bear": bear, "verdict": verdict}


def render(result: dict) -> str:
    """把辩论结果渲染成可读文本。"""
    return (f"## 🟢 多头\n{result['bull']}\n\n"
            f"## 🔴 空头\n{result['bear']}\n\n"
            f"## ⚖️ 风控裁决\n{result['verdict']}\n")

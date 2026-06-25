"""真 agent loop 深度研究(P2，借鉴 deer-flow 精髓:plan-execute 动态决策)。

精髓在"真 loop":LLM 每轮看已收集证据，自己决定下一个 action(查库/取行情/取财务/done)，
而非硬编码固定拆解。工具 kb(本地库 ask)内置；quote/finance(tdx/ifind)可注入，
不硬依赖外部取数。complete 与 tools 可注入(测试用 mock 验证 loop 动态决策与终止)。
"""
from __future__ import annotations

import json
import re

from . import llm as _llm

_PLAN = """你是投研研究规划员。把下面的问题拆成 3-5 个需要查证的子问题。
问题：{query}
只输出 JSON 数组字符串，如 ["子问题1","子问题2"]："""

_STEP = """你在做投研深度研究。目标问题：{query}

已收集的证据：
{evidence}

决定下一步 action（只输出一个 JSON 对象，不要解释）：
{{"action": "kb"|"quote"|"finance"|"done", "arg": "查询词或股票代码", "why": "原因"}}
- kb: 查本地知识库(arg=查询词)
- quote: 取最新行情(arg=股票代码如 688627)
- finance: 取财务数据(arg=股票代码)
- done: 证据已足够，停止
JSON："""

_SUMMARY = """基于以下分步收集的证据，综合回答问题。只用证据中的信息，标注成色(A/B/C/D)，
低成色/未兑现预期要提示别当独立买点。
问题：{query}

证据：
{evidence}

综合回答："""


def _extract_json(text: str) -> str:
    """从 LLM 输出里抽第一个**完整**JSON 对象/数组(容忍 ```json 围栏与前后解释)。

    用括号配平扫描而非贪婪正则：贪婪 `\\{.*\\}` 会从首个 `{` 一路吃到末个 `}`，
    当 LLM 在 JSON 前后带解释、或正文含第二对花括号时，截出夹带散文的非法串 →
    json.loads 抛错 → deep_ask 第一步就 break，真 loop 名存实亡。配平扫描只取
    第一个深度归零的完整对象，并跳过字符串字面量内的括号。
    """
    if not text:
        return ""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)   # 先剥 ```json 围栏
    if fence:
        text = fence.group(1)
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return ""
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = esc = False
    for j in range(start, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return ""


def _run_tool(action: str, arg: str, engine, tools: dict) -> str:
    """执行一个 action。kb 内置;quote/finance 走注入的工具，未接入则提示。"""
    if action == "kb":
        return engine.ask(arg).to_six_section()
    fn = tools.get(action)
    if fn:
        try:
            return str(fn(arg))
        except Exception as e:
            return f"(工具 {action} 失败: {e})"
    return f"(工具 {action} 未接入)"


def deep_ask(query: str, engine, tools=None, complete=None, max_steps: int = 6) -> dict:
    """真 agent loop:plan → 每轮动态决定 action → 执行 → 回灌 → 汇总。

    engine    AskEngine(kb 工具)。
    tools     {"quote": fn, "finance": fn} 可选注入(取数包装)，不注入则该 action 提示未接入。
    complete  LLM 补全函数；默认降级链，可注入 mock。
    返回 {plan, steps, evidence, answer}。
    """
    complete = complete or _llm.complete
    tools = tools or {}
    evidence: list[str] = []
    plan = complete(_PLAN.format(query=query), max_tokens=400) or "[]"
    evidence.append(f"[计划] {plan}")
    steps: list[tuple] = []
    for _ in range(max_steps):
        raw = complete(_STEP.format(query=query, evidence="\n".join(evidence)[:6000]),
                       max_tokens=200) or "{}"
        try:
            act = json.loads(_extract_json(raw))
        except Exception:
            break
        action, arg = act.get("action"), act.get("arg", "")
        steps.append((action, arg))
        if action == "done" or not action:
            break
        evidence.append(f"[{action}:{arg}]\n{_run_tool(action, arg, engine, tools)[:800]}")
    answer = complete(_SUMMARY.format(query=query, evidence="\n".join(evidence)[:7000]),
                      max_tokens=1800, tier="answer") or "(无输出)"
    return {"plan": plan, "steps": steps, "evidence": evidence, "answer": answer}

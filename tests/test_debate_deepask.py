"""P2 真对抗 / 真 loop 测试(mock LLM，验证编排而非真调模型)。"""
from trading_kb.debate import debate, render
from trading_kb.deep_ask import _extract_json, deep_ask


class _FakeAsk:
    def to_six_section(self):
        return "## 结论\n精智达国产存储测试 [C级]\n"


class _FakeEngine:
    def ask(self, q):
        return _FakeAsk()


def test_debate_is_real_adversarial():
    """真对抗:空头必须看到多头论点、风控必须看到双方(非各写一段拼接)。"""
    seen = {}

    def fc(prompt, max_tokens=0, tier="extract"):
        if "空头反驳与独立论点" in prompt:
            seen["bear_saw_bull"] = "多头A:国产替代独苗" in prompt
            return "空头反驳:成色仅C，净利未兑现"
        if "风控裁决" in prompt:
            seen["judge_saw_both"] = ("多头A:国产替代独苗" in prompt
                                      and "空头反驳:成色仅C" in prompt)
            return "裁决:方向对但贵"
        if "看多论点" in prompt:
            return "多头A:国产替代独苗[C]"
        return ""

    r = debate("精智达", _FakeEngine(), complete=fc)
    assert seen["bear_saw_bull"] is True        # 空头确实读到多头论点
    assert seen["judge_saw_both"] is True        # 风控确实读到双方
    assert "裁决" in r["verdict"]
    assert "多头" in render(r) and "空头" in render(r)


def test_deep_ask_dynamic_loop_and_done():
    """真 loop:依次 kb→finance→done，工具真被调用，done 终止，kb 结果回灌。"""
    script = iter([
        '["子问题1"]',                                       # plan
        '{"action":"kb","arg":"精智达逻辑","why":"查库"}',       # step1
        '{"action":"finance","arg":"688627","why":"查净利"}',   # step2
        '{"action":"done","arg":"","why":"够了"}',              # step3 终止
        "综合结论:逻辑硬但净利未兑现[B]",                          # summary
    ])

    def fc(prompt, max_tokens=0, tier="extract"):
        try:
            return next(script)
        except StopIteration:
            return ""

    tool_calls = []
    tools = {"finance": lambda arg: tool_calls.append(arg) or f"净利0.65亿(arg={arg})"}
    r = deep_ask("精智达", _FakeEngine(), tools=tools, complete=fc)

    assert ("kb", "精智达逻辑") in r["steps"]
    assert ("finance", "688627") in r["steps"]
    assert ("done", "") in r["steps"]
    assert tool_calls == ["688627"]              # finance 工具真被调用
    assert "综合结论" in r["answer"]
    assert any("国产存储测试" in e for e in r["evidence"])   # kb 结果回灌进证据


def test_deep_ask_stops_at_max_steps():
    """非 done 但达上限应停止(防 loop 不收敛)。"""
    def fc(prompt, max_tokens=0, tier="extract"):
        if "JSON" in prompt:
            return '{"action":"kb","arg":"x","why":"循环"}'   # 永不 done
        return "最终"
    r = deep_ask("q", _FakeEngine(), complete=fc, max_steps=3)
    assert len(r["steps"]) == 3                  # 被 max_steps 截断


def test_extract_json_balanced_not_greedy():
    """🔴回归:JSON 前后带散文/第二对花括号/字符串内括号/```围栏，都要抽出第一个完整 JSON。

    旧贪婪正则 `\\{.*\\}` 会从首 { 吃到末 }，截出夹带散文的非法串 → json.loads 抛错。
    """
    import json
    samples = [
        ('我先查库。{"action":"kb","arg":"精智达"}。然后 {"action":"done"}。',
         {"action": "kb", "arg": "精智达"}),
        ('```json\n{"action":"finance","arg":"688627"}\n``` 决定如上',
         {"action": "finance", "arg": "688627"}),
        ('{"action":"kb","arg":"含{大括号}的字符串","why":"x"}',
         {"action": "kb", "arg": "含{大括号}的字符串", "why": "x"}),
        ('思考...["子问题1","子问题2"] 完毕', ["子问题1", "子问题2"]),
    ]
    for raw, want in samples:
        assert json.loads(_extract_json(raw)) == want


def test_deep_ask_loop_runs_with_prose_wrapped_json():
    """🔴回归:真实风格(带解释)的 LLM 输出也要驱动 loop 真执行，而非第一步就空转 break。"""
    script = iter([
        '我先拆解。["精智达逻辑","净利兑现"]',                                  # plan
        '让我查库。{"action":"kb","arg":"精智达","why":"查逻辑"}',               # step1
        '证据够了。{"action":"done","arg":"","why":"足够"}',                    # done
        "综合结论:逻辑硬[B]",                                                   # summary
    ])

    def fc(prompt, max_tokens=0, tier="extract"):
        try:
            return next(script)
        except StopIteration:
            return ""
    r = deep_ask("精智达", _FakeEngine(), complete=fc)
    assert ("kb", "精智达") in r["steps"]         # 散文包裹的 action 仍被执行
    assert ("done", "") in r["steps"]

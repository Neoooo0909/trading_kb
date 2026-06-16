"""质疑模块 + 联网权威信源测试。"""
import pytest

from trading_kb.critique import CritiqueEngine, _extract_metrics, _backtest_years, _parse_num
from trading_kb.models import Finding
from trading_kb import web_enrich, config


def _f(claim, evidence="", numbers=None):
    return Finding(claim=claim, evidence=evidence, numbers=numbers or [])


# ── 指标抽取 ──────────────────────────────────────────────────────────────
def test_extract_metrics():
    f = _f("因子表现", numbers=[
        {"value": "40.12%", "context": "多空组合年化收益率"},
        {"value": "-9.73%", "context": "Rank IC"},
        {"value": "4.51", "context": "信息比率"}])
    m = dict(_extract_metrics(f))
    assert m["annual_return"] == 40.12
    assert m["ic"] == 9.73          # 取绝对值
    assert m["info_ratio"] == 4.51


def test_parse_and_years():
    assert _parse_num("33.05%") == 33.05
    assert _backtest_years("回测2013.1-2023.10") == 10.0
    assert _backtest_years("无区间") is None


# ── ① 无出处 / 推测 ──────────────────────────────────────────────────────
def test_no_source_flag():
    eng = CritiqueEngine()
    res = eng.critique(_f("某结论", evidence="", numbers=[]))
    assert any(fl.kind == "no_source" for fl in res.flags)


def test_speculative_flag():
    eng = CritiqueEngine()
    res = eng.critique(_f("预计明年有望大幅增长", evidence="", numbers=[]))
    kinds = {fl.kind for fl in res.flags}
    assert "speculative" in kinds or "no_source" in kinds


# ── ② 过于乐观(分位对照)────────────────────────────────────────────────
def test_over_optimistic_outlier():
    # 构造一批年化数据:大多 10~20,一条 90(离群乐观)
    train = [_f(f"因子{i}", numbers=[{"value": f"{10+i}%", "context": "年化收益率"}])
             for i in range(20)]
    eng = CritiqueEngine().fit(train)
    res = eng.critique(_f("神因子", numbers=[{"value": "90%", "context": "年化收益率"}]))
    assert any(fl.kind == "over_optimistic" for fl in res.flags)


def test_not_optimistic_when_normal():
    train = [_f(f"因子{i}", numbers=[{"value": f"{10+i}%", "context": "年化收益率"}])
             for i in range(20)]
    eng = CritiqueEngine().fit(train)
    res = eng.critique(_f("普通因子", numbers=[{"value": "12%", "context": "年化收益率"}]))
    assert not any(fl.kind == "over_optimistic" for fl in res.flags)


# ── ③ 回测软肋 ────────────────────────────────────────────────────────────
def test_backtest_no_out_of_sample():
    eng = CritiqueEngine()
    res = eng.critique(_f("回测显示该因子表现优异"))
    assert any(fl.kind == "backtest_weak" for fl in res.flags)


def test_backtest_short_period():
    eng = CritiqueEngine()
    res = eng.critique(_f("回测2022-2023年表现好"))   # 仅1年
    assert any(fl.kind == "backtest_weak" and "样本偏短" in fl.message for fl in res.flags)


def test_out_of_sample_no_backtest_flag():
    eng = CritiqueEngine()
    res = eng.critique(_f("回测并经样本外滚动验证均稳健"))
    assert not any(fl.kind == "backtest_weak" and "样本外" in fl.message for fl in res.flags)


# ── 联网权威信源 ─────────────────────────────────────────────────────────
def test_web_disabled_by_default():
    assert web_enrich.make_announcement_verifier() is None
    assert web_enrich.make_corroborator() is None


def test_authoritative_whitelist():
    assert web_enrich.is_authoritative("cls.cn") is True
    assert web_enrich.is_authoritative("www.sse.com.cn") is True
    assert web_enrich.is_authoritative("xueqiu.com") is False     # 非权威不采信
    assert web_enrich.is_authoritative("某自媒体.com") is False


def test_web_enabled_hooks_exist(monkeypatch):
    monkeypatch.setattr(config, "USE_WEB", True)
    assert web_enrich.make_announcement_verifier() is not None
    # 安全桩:查无返回 None,绝不假装确认
    v = web_enrich.make_announcement_verifier()
    assert v(_f("某公司中标", numbers=[]), "HAS_CONFIRMED_ORDER") is None

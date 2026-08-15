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


def test_ic_word_boundary_no_english_false_hits():
    """回归锚(2026-08-16):裸 "ic" 子串曾命中 price/historical 等英文语境,
    外资行研报目标价被当 IC 计入分布 → 阈值污染 + 荒谬 doubt 落库。"""
    f = _f("外资行观点", numbers=[
        {"value": "2820", "context": "price target NT$2,820"},
        {"value": "60%", "context": "historical excess return"},
    ])
    m = dict(_extract_metrics(f))
    assert "ic" not in m
    assert "icir" not in m
    # 真 IC 语境(中英文)仍要命中
    f2 = _f("因子", numbers=[
        {"value": "0.05", "context": "IC均值"},
        {"value": "-9.73%", "context": "Rank IC"},
        {"value": "该因子ic为", "context": "该因子 ic 显著"},
    ])
    m2 = dict(_extract_metrics(f2))
    assert m2["ic"] in (0.05, 9.73)     # 至少命中(同指标取后到者,dict 覆盖)


def test_ic_cap_rejects_implausible_values():
    """值域护栏:|IC|>100 视为误抽(价格/市值),不入分布。"""
    f = _f("x", numbers=[{"value": "2820", "context": "rank ic 2820"}])
    assert dict(_extract_metrics(f)) == {}


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


def test_web_enabled_hooks_exist(monkeypatch):
    monkeypatch.setattr(config, "USE_WEB", True)
    assert web_enrich.make_announcement_verifier() is not None
    # 无主体实体 → 未尝试(None),绝不假装确认也不假装查过
    v = web_enrich.make_announcement_verifier()
    assert v(_f("某公司中标", numbers=[]), "HAS_CONFIRMED_ORDER") is None


# ── verifier 三态协议(ARCHITECTURE.md §2.1)────────────────────────────────
class _Ann:
    def __init__(self, title):
        self.title = title


def _f_ent(claim):
    return Finding(claim=claim, entities=["某某科技"])


def test_verifier_confirms_only_on_topic_match(monkeypatch):
    """回归锚:v0.4 前"存在任意公告"即 confirmed → 任意公告洗白成 A 级。
    现在必须命中谓词主题词才 confirmed。"""
    from trading_kb import announcement
    monkeypatch.setattr(config, "USE_WEB", True)
    monkeypatch.setattr(announcement, "fetch_announcements",
                        lambda *a, **k: [_Ann("关于召开2026年股东大会的通知")])
    v = web_enrich.make_announcement_verifier()
    # 有公告但与订单无关 → 真查无(no_evidence),而非 confirmed
    assert v(_f_ent("公司中标大单"), "HAS_CONFIRMED_ORDER") == "no_evidence"

    monkeypatch.setattr(announcement, "fetch_announcements",
                        lambda *a, **k: [_Ann("关于中标某项目的公告")])
    assert v(_f_ent("公司中标大单"), "HAS_CONFIRMED_ORDER") == "confirmed"


def test_verifier_none_on_failure_or_unsupported(monkeypatch):
    from trading_kb import announcement
    monkeypatch.setattr(config, "USE_WEB", True)
    v = web_enrich.make_announcement_verifier()
    # 请求异常 → None(未尝试,不降级)
    def _boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(announcement, "fetch_announcements", _boom)
    assert v(_f_ent("公司中标大单"), "HAS_CONFIRMED_ORDER") is None
    # 价格信号无公告验证动作 → None
    assert v(_f_ent("股价异动"), "HAS_PRICE_SIGNAL") is None

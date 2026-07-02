"""环境感知重估引擎(revalue,C)——纯逻辑离线测试。

只测不联网的纯逻辑:框架判定 / 快慢变量分类 / 分位 / 代码归一 / 渲染 / 降级契约。
取数 I/O(build_env 里的 ifind 调用)不在单测覆盖内(需实时行情,不可离线复现)。
"""
from trading_kb.revalue import (
    EnvSnapshot, frame_verdict, classify_fact_speed, reweight_note,
    _percentile, _norm_codes, render_section, build_env,
)


# ── 代码归一 ─────────────────────────────────────────────────────────────────
def test_norm_codes_variants():
    assert _norm_codes("SH603690") == ("sh603690", "603690.SH")
    assert _norm_codes("603690.SH") == ("sh603690", "603690.SH")
    assert _norm_codes("603690") == ("sh603690", "603690.SH")     # 6 开头→SH
    assert _norm_codes("000001") == ("sz000001", "000001.SZ")     # 0 开头→SZ
    assert _norm_codes("430047") == ("bj430047", "430047.BJ")     # 4 开头→BJ
    assert _norm_codes("garbage") == ("", "")


# ── 分位 ─────────────────────────────────────────────────────────────────────
def test_percentile():
    assert _percentile([1, 2, 3, 4, 5], 4) == 60.0
    assert _percentile([1, 2, 3, 4, 5], 1) == 0.0
    assert _percentile([], 1) == 0.0


# ── 快/慢变量分类(时效衰减速度,与成色正交)────────────────────────────────────
def test_classify_fact_speed():
    assert classify_fact_speed("公司计提减值准备2.99亿") == "快变量"
    assert classify_fact_speed("大股东减持股份计划") == "快变量"
    assert classify_fact_speed("新签订单58亿元") == "快变量"
    assert classify_fact_speed("湿法设备四大技术平台") == "慢变量"
    assert classify_fact_speed("主要客户结构与国产替代战略") == "慢变量"
    assert classify_fact_speed("公司注册地在上海") == "中性"


# ── 框架判定(第一性原理:盈利是 PE 框架前提)──────────────────────────────────
def test_frame_distress_reversal():
    """亏损 + PB 高分位 = 困境反转/主题定价,基本面权重低(至纯案例)。"""
    snap = EnvSnapshot(pe_ttm=-16.9, loss_making=True, loss_frac_3y=34,
                       pb=3.45, pb_pct_3y=98, ret_5d=16.8, ret_10d=27.9,
                       range_pct_250=78, vol_ratio=2.5, alpha_10d=26)
    v = frame_verdict(snap)
    assert v["frame"] == "困境反转 / 主题定价"
    assert v["fundamental_weight"] == "低"
    assert any("PE" in r for r in v["reasons"])


def test_frame_value_lowpb():
    """盈利 + PB 低分位 = 价值/低估,基本面权重高。"""
    v = frame_verdict(EnvSnapshot(pe_ttm=15, loss_making=False, pb=1.2,
                                  pb_pct_3y=12, ret_10d=2))
    assert v["frame"] == "价值 / 低估修复"
    assert v["fundamental_weight"] == "高"


def test_frame_sector_beta():
    """盈利 + 显著跑赢 + 放量 = 板块 beta/主题驱动,基本面权重低。"""
    v = frame_verdict(EnvSnapshot(pe_ttm=40, loss_making=False, pb=5,
                                  pb_pct_3y=60, ret_10d=30, alpha_10d=25, vol_ratio=2))
    assert v["frame"] == "板块 beta / 主题驱动"
    assert v["fundamental_weight"] == "低"


def test_frame_insufficient_data_conservative():
    """无量价无估值 → 保守判"数据不足",绝不臆断。"""
    v = frame_verdict(EnvSnapshot())
    assert "数据不足" in v["frame"]


def test_peer_alpha_over_market_alpha():
    """有同业α时优先用同业α判 beta(分离板块β vs 个股α)。

    相对沪深300 看似高α(+26),但相对同业实为跑输(-22)→不应据大盘α判个股独强。
    """
    snap = EnvSnapshot(pe_ttm=-16.9, loss_making=True, pb=3.45, pb_pct_3y=98,
                       ret_10d=27.9, alpha_10d=26, peer_alpha_10d=-22, vol_ratio=2.5)
    v = frame_verdict(snap)
    # 亏损+高PB分位仍先判困境反转(优先级最高),但基本面权重=低一致
    assert v["fundamental_weight"] == "低"


# ── 重加权提示 ───────────────────────────────────────────────────────────────
def test_reweight_downweights_fast_when_low_fundamental():
    facts = [{"claim": "计提减值2.99亿"}, {"claim": "大股东减持"},
             {"claim": "四大技术平台"}]
    rw = reweight_note(facts, {"fundamental_weight": "低"})
    assert rw["fast_n"] == 2 and rw["slow_n"] == 1
    assert rw["downweight_fast"] is True


def test_reweight_no_downweight_when_high_fundamental():
    rw = reweight_note([{"claim": "计提减值"}], {"fundamental_weight": "高"})
    assert rw["downweight_fast"] is False


# ── 渲染 ─────────────────────────────────────────────────────────────────────
def test_render_section_contains_key_signals():
    snap = EnvSnapshot(pe_ttm=-16.9, loss_making=True, loss_frac_3y=34, pb=3.45,
                       pb_pct_3y=98, ret_5d=16.8, ret_10d=27.9, range_pct_250=78,
                       vol_ratio=2.5, alpha_10d=26, peer_alpha_10d=-22,
                       peer_note="同业4只中位近10日+50.0%")
    s = render_section(snap, [{"claim": "计提减值2.99亿"}, {"claim": "技术平台"}])
    assert "环境重估" in s
    assert "困境反转" in s
    assert "PE 锚失效" in s
    assert "快变量" in s
    assert "相对同业" in s        # 同业α已渲染(分离板块β)


# ── 降级契约 ─────────────────────────────────────────────────────────────────
def test_build_env_non_security_returns_none():
    """非证券(无 6 位码)不触发取数,返回 None。"""
    assert build_env("concept:半导体设备") is None
    assert build_env("") is None

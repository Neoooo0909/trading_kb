"""环境感知重估引擎(C)：把"存量事实"放进"当前市场坐标系"重新加权。

动机(design 缺口)：tkb 的六段结论只沉淀个股基本面，但当前股价往往由
【板块 beta + 定价框架切换 + 估值极值】主导——事实层对这些的解释力接近零，
导致"交易含义"是**悬空重估**(逻辑自洽但没锚在当下坐标系)。

本模块补三样，与既有"成色/时效"两轴正交：
1. EnvSnapshot —— 拉当前环境(量价位置 / 估值分位+盈亏 / 相对大盘α / 同业池)；
2. frame_verdict —— 从数据推当前该用哪套定价框架(成长PE / 困境反转PB / 主题beta / 价值低估)；
3. reweight_facts —— 给事实按快/慢变量分层，据框架调"当前相关性"权重。

设计原则(对齐 config.USE_WEB 范式)：**默认不参与 ask**(离线可复现)，仅 TKB_REVALUE=1
或 `--revalue` 显式启用；取数全部 graceful degrade(拿不到就置 None，绝不抛错阻断问答)；
纯逻辑(frame_verdict / classify_fact_speed / _percentile)与取数 I/O 分离，保证可离线单测。

取数直连不走代理：tdx.py / ifind_ft.py 自带连接管理，此处只导入调用(见 memory
feedback_data_scripts_direct_connect)；绝不自启 iFinD 客户端(复用运行中的会话)。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date as _Date
from pathlib import Path
from typing import Optional

# ── 快/慢变量关键词(第一性原理：按"时效衰减速度"分层，不按成色)────────────────
# 快变量：高时效敏感、衰减快——单季/单次事件，几周内就被新事件覆盖或 price in。
_FAST_KW = ("减值", "减持", "增持", "回购", "订单", "中标", "定点", "交付", "出货",
            "业绩预告", "业绩快报", "预亏", "预增", "涨停", "跌停", "资金", "解禁",
            "分红", "股权质押", "质押", "诉讼", "处罚", "问询", "更正")
# 慢变量：结构性、衰减慢——护城河/商业模式类，一两年后大概率仍成立。
_SLOW_KW = ("技术平台", "客户结构", "商业模式", "战略", "护城河", "产能布局",
            "研发", "行业地位", "供应商", "工艺", "产品线", "专利", "认证", "国产替代")


@dataclass
class EnvSnapshot:
    """标的当前市场环境快照。任一字段为 None 表示该维度取数失败(优雅降级)。"""
    tdx_code: str = ""
    ifind_code: str = ""
    last: Optional[float] = None            # 最新价
    ret_5d: Optional[float] = None          # 近5日涨幅(%)
    ret_10d: Optional[float] = None         # 近10日涨幅(%)
    range_pct_250: Optional[float] = None   # 近250日区间分位(%)：(last-low)/(high-low)
    vol_ratio: Optional[float] = None       # 近5日均量 / 近60日均量(量能是否放大)
    pb: Optional[float] = None
    pb_pct_3y: Optional[float] = None       # PB 近三年分位(%)
    pe_ttm: Optional[float] = None
    loss_making: Optional[bool] = None      # 当前是否亏损(PE<0)
    loss_frac_3y: Optional[float] = None    # 近三年亏损天数占比(%)
    alpha_10d: Optional[float] = None       # 近10日相对沪深300超额(%)
    peer_alpha_10d: Optional[float] = None  # 近10日相对同业中位超额(%)；无同业池则 None
    peer_note: str = ""                     # 同业口径说明
    errors: list[str] = field(default_factory=list)

    def has_price(self) -> bool:
        return self.ret_10d is not None

    def has_val(self) -> bool:
        return self.pb is not None


# ── 取数 I/O(全部 graceful degrade)──────────────────────────────────────────
def _norm_codes(cid: str) -> tuple[str, str]:
    """canonical_id → (tdx_code, ifind_code)。

    接受 'SH603690' / '603690.SH' / '603690'。tdx 用小写前缀 sh/sz/bj；ifind 用 600xxx.SH。
    6/9 开头→SH，0/2/3 开头→SZ，4/8 开头→BJ。
    """
    s = (cid or "").upper()
    m = re.search(r"(SH|SZ|BJ)?\D*(\d{6})", s)
    if not m:
        return "", ""
    mkt, num = m.group(1), m.group(2)
    if not mkt:
        mkt = "SH" if num[0] in "69" else ("BJ" if num[0] in "48" else "SZ")
    return f"{mkt.lower()}{num}", f"{num}.{mkt}"


def _ifind():
    """懒加载 iFindFT(直连,自带 jgbsessid/CDP 认证与刷新)。"""
    if str(Path.home()) not in sys.path:
        sys.path.insert(0, str(Path.home()))
    from ifind_ft import iFindFT
    return iFindFT()


def _series(df, col):
    """DataFrame 某列 → 数值 Series(去 NaN);缺列/空表返回空 Series。"""
    import pandas as pd
    if df is None or getattr(df, "empty", True) or col not in df:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce").dropna()


def _ret(close, n: int) -> Optional[float]:
    """收盘 Series 近 n 个交易日涨幅(%);样本不足返回 None。"""
    if len(close) < n + 1:
        return None
    return round(100 * (close.iloc[-1] / close.iloc[-(n + 1)] - 1), 1)


def _fetch_stock(ft, snap: EnvSnapshot) -> None:
    """主标的:一次 hxds 取近三年 收盘/成交量/PB/PE → 量价位置 + 估值分位 + 盈亏状态。

    量价改走 ifind(非 tdx):tdx 大 count 日K 易超时且连接一旦超时会污染后续调用(实测),
    ifind hxds 稳定(~1s)且指数/多标的通吃。PE_TTM<0=亏损→成长/PE框架失效(见 frame_verdict)。
    """
    start = f"{_Date.today().year - 3}-01-01"
    end = _Date.today().isoformat()
    df = ft.hxds(snap.ifind_code,
                 ["ths_close_price_stock", "ths_vol_stock",
                  "ths_pb_latest_stock", "ths_pe_ttm_stock"],
                 start, end, interval="D")
    close = _series(df, "ths_close_price_stock")
    vol = _series(df, "ths_vol_stock")
    pb = _series(df, "ths_pb_latest_stock")
    pe = _series(df, "ths_pe_ttm_stock")
    if len(close) >= 11:
        snap.last = round(float(close.iloc[-1]), 2)
        snap.ret_5d = _ret(close, 5)
        snap.ret_10d = _ret(close, 10)
        c250 = close.tail(250)
        hi, lo = float(c250.max()), float(c250.min())
        if hi > lo:
            snap.range_pct_250 = round(100 * (snap.last - lo) / (hi - lo), 0)
    if len(vol) >= 60:
        v5, v60 = vol.tail(5).mean(), vol.tail(60).mean()
        if v60:
            snap.vol_ratio = round(float(v5 / v60), 2)
    if len(pb) > 10:
        snap.pb = round(float(pb.iloc[-1]), 2)
        snap.pb_pct_3y = round(_percentile(pb.tolist(), pb.iloc[-1]), 0)
    if len(pe) > 10:
        snap.pe_ttm = round(float(pe.iloc[-1]), 2)
        snap.loss_making = bool(pe.iloc[-1] < 0)
        snap.loss_frac_3y = round(100 * (pe < 0).mean(), 0)


def _fetch_beta(ft, snap: EnvSnapshot, peers: list[str] | None) -> None:
    """相对沪深300 与相对同业的近10日超额(α)。best-effort,失败静默不影响主判定。

    同业α是分离"板块β vs 个股α"的关键(至纯案例:相对沪深300超额其实是板块β)。
    """
    if snap.ret_10d is None:
        return
    start = f"{_Date.today().year - 1}-11-01"          # 覆盖 >10 个交易日
    end = _Date.today().isoformat()
    try:                                               # 沪深300 基准(指数走 stock 收盘指标可取)
        b10 = _ret(_series(ft.hxds("000300.SH", ["ths_close_price_stock"],
                                   start, end, interval="D"), "ths_close_price_stock"), 10)
        if b10 is not None:
            snap.alpha_10d = round(snap.ret_10d - b10, 1)
    except Exception:
        pass
    pcodes = [_norm_codes(p)[1] for p in (peers or [])]
    pcodes = [c for c in pcodes if c and c != snap.ifind_code]
    rets = []
    for pc in pcodes:                                  # 逐码取(格式最稳),数量受同业池限制
        try:
            r = _ret(_series(ft.hxds(pc, ["ths_close_price_stock"], start, end,
                                     interval="D"), "ths_close_price_stock"), 10)
            if r is not None:
                rets.append(r)
        except Exception:
            continue
    if rets:
        rets.sort()
        med = rets[len(rets) // 2]
        snap.peer_alpha_10d = round(snap.ret_10d - med, 1)
        snap.peer_note = f"同业{len(rets)}只中位近10日{med:+.1f}%"


# ── 纯逻辑(可离线单测)────────────────────────────────────────────────────────
def _percentile(series: list[float], value: float) -> float:
    """value 在 series 中的分位(%)：严格小于 value 的占比 ×100。"""
    if not series:
        return 0.0
    return 100.0 * sum(1 for x in series if x < value) / len(series)


def frame_verdict(snap: EnvSnapshot) -> dict:
    """从环境快照推当前定价框架 + 基本面权重档 + 理由。

    第一性原理(不靠约定，靠机制)：
    - 盈利是 PE/成长框架的前提；亏损时 PE 无意义，市场只能用 PB/反转/主题框架；
    - PB 处高分位却在亏损 = 市场为"预期修复"付溢价，而非当期盈利 → 困境反转/主题定价；
    - 近10日大幅跑赢大盘/同业 + 放量 = beta/主题驱动，个股基本面短期解释力被稀释；
    - PB 低分位 + 正常盈利 = 价值/低估框架，基本面权重高。

    返回 {frame, fundamental_weight('低'/'中'/'高'), reasons:[...] }。
    数据不足时给保守的"数据不足"判定，绝不臆断。
    """
    reasons: list[str] = []
    if not snap.has_price() and not snap.has_val():
        return {"frame": "数据不足(未取到量价/估值)", "fundamental_weight": "中", "reasons": []}

    loss = snap.loss_making is True
    pb_hi = snap.pb_pct_3y is not None and snap.pb_pct_3y >= 80
    pb_lo = snap.pb_pct_3y is not None and snap.pb_pct_3y <= 30
    # beta/主题信号：显著跑赢基准(优先看同业α，无则看大盘α) + 放量
    a = snap.peer_alpha_10d if snap.peer_alpha_10d is not None else snap.alpha_10d
    hot = (snap.ret_10d is not None and snap.ret_10d >= 15)
    vol_up = snap.vol_ratio is not None and snap.vol_ratio >= 1.3

    if loss:
        reasons.append(f"当前亏损(PE_TTM={snap.pe_ttm})→成长/PE 框架失效，无当期盈利锚")
    if pb_hi and loss:
        frame = "困境反转 / 主题定价"
        reasons.append(f"PB={snap.pb}处近三年{snap.pb_pct_3y:.0f}%高分位却在亏损"
                       "→市场在为'预期修复/主题'付溢价而非当期盈利")
        fund = "低"
    elif hot and (a is None or a >= 10):
        frame = "板块 beta / 主题驱动"
        if a is not None:
            reasons.append(f"近10日{'相对同业' if snap.peer_alpha_10d is not None else '相对沪深300'}"
                           f"超额{a:+.1f}%→beta/主题主导，个股基本面短期解释力弱")
        fund = "低"
    elif pb_lo and not loss:
        frame = "价值 / 低估修复"
        reasons.append(f"PB 处近三年{snap.pb_pct_3y:.0f}%低分位且盈利为正→基本面权重高")
        fund = "高"
    elif loss:
        frame = "困境反转(待盈利拐点)"
        fund = "低"
    else:
        frame = "常态基本面定价"
        fund = "中"

    if vol_up:
        reasons.append(f"量能放大(近5日均量/60日={snap.vol_ratio}x)→资金驱动特征")
    if snap.range_pct_250 is not None:
        pos = "高位" if snap.range_pct_250 >= 70 else ("低位" if snap.range_pct_250 <= 30 else "中位")
        reasons.append(f"股价处近一年{pos}({snap.range_pct_250:.0f}%分位)")
    return {"frame": frame, "fundamental_weight": fund, "reasons": reasons}


def classify_fact_speed(claim: str) -> str:
    """按关键词把事实分 '快变量'/'慢变量'/'中性'(时效衰减速度分层，与成色正交)。"""
    t = claim or ""
    if any(k in t for k in _FAST_KW):
        return "快变量"
    if any(k in t for k in _SLOW_KW):
        return "慢变量"
    return "中性"


def reweight_note(facts: list[dict], verdict: dict) -> dict:
    """据框架给事实分快/慢变量并出重加权提示。

    核心规则：当框架判定"基本面权重=低"(beta/主题/困境反转主导)时，个股快变量基本面
    (订单/交付/减值等)对**当前股价**的解释力被稀释，应在交易含义里下调其即时权重、
    标注"当前由框架/beta 主导"；慢变量(护城河类)不受影响。返回渲染所需的分组计数。
    """
    fast = [f for f in facts if classify_fact_speed(f.get("claim", "")) == "快变量"]
    slow = [f for f in facts if classify_fact_speed(f.get("claim", "")) == "慢变量"]
    downweight = verdict.get("fundamental_weight") == "低"
    return {"fast_n": len(fast), "slow_n": len(slow),
            "downweight_fast": downweight,
            "fast_samples": [f.get("claim", "")[:36] for f in fast[:4]]}


# ── 编排 + 渲染 ──────────────────────────────────────────────────────────────
def build_env(cid: str, facts: list[dict] | None = None,
              peers: list[str] | None = None, timeout: float = 45.0) -> Optional[EnvSnapshot]:
    """给证券 cid 构建环境快照(量价+估值+相对强弱)。

    取数在 daemon 线程内跑并 join(timeout):env 是可选增强,**绝不能因取数卡住拖死问答**——
    超时即返回已拿到的部分快照(拿不到任何维度则 None)。线程为 daemon,随进程退出,不泄漏。
    """
    import threading
    tdx_code, ifind_code = _norm_codes(cid)
    if not ifind_code:
        return None
    snap = EnvSnapshot(tdx_code=tdx_code, ifind_code=ifind_code)

    def _io() -> None:
        try:
            ft = _ifind()
        except Exception as e:
            snap.errors.append(f"ifind:{type(e).__name__}")
            return
        try:
            _fetch_stock(ft, snap)
        except Exception as e:
            snap.errors.append(f"stock:{type(e).__name__}")
        try:
            _fetch_beta(ft, snap, peers)
        except Exception:
            pass

    th = threading.Thread(target=_io, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        snap.errors.append(f"timeout>{timeout:.0f}s(部分维度未回)")
    if not snap.has_price() and not snap.has_val():     # 量价+估值全没 → 无重估价值
        return None
    return snap


def render_section(snap: EnvSnapshot, facts: list[dict]) -> str:
    """渲染六段骨架的「🌡 环境重估」段：环境快照 + 框架判定 + 快慢变量重加权提示。"""
    v = frame_verdict(snap)
    rw = reweight_note(facts, v)
    L = ["\n## 🌡 环境重估(当前定价框架)",
         "(C·环境感知重估：把上面的存量事实放进当下市场坐标系；数据为实时取数，"
         "与事实成色正交)"]

    # 环境快照
    q = []
    if snap.ret_10d is not None:
        q.append(f"近5/10日 {snap.ret_5d:+.1f}%/{snap.ret_10d:+.1f}%")
    if snap.range_pct_250 is not None:
        q.append(f"近一年区间 {snap.range_pct_250:.0f}% 分位")
    if snap.vol_ratio is not None:
        q.append(f"量能 {snap.vol_ratio}x")
    if snap.alpha_10d is not None:
        q.append(f"相对沪深300 α(10d) {snap.alpha_10d:+.1f}%")
    if snap.peer_alpha_10d is not None:
        q.append(f"相对同业 α(10d) {snap.peer_alpha_10d:+.1f}%({snap.peer_note})")
    if q:
        L.append(f"- **量价**：{'；'.join(q)}")
    val = []
    if snap.pb is not None:
        val.append(f"PB {snap.pb}(近三年 {snap.pb_pct_3y:.0f}% 分位)")
    if snap.pe_ttm is not None:
        if snap.loss_making:
            val.append(f"PE_TTM {snap.pe_ttm}（**亏损**，近三年 {snap.loss_frac_3y:.0f}% 时间亏损→PE 锚失效）")
        else:
            val.append(f"PE_TTM {snap.pe_ttm}")
    if val:
        L.append(f"- **估值**：{'；'.join(val)}")

    # 框架判定
    L.append(f"- **当前定价框架**：**{v['frame']}**（个股基本面权重：{v['fundamental_weight']}）")
    for r in v["reasons"]:
        L.append(f"    · {r}")

    # 快/慢变量重加权
    L.append(f"- **事实重加权**：快变量 {rw['fast_n']} 条 / 慢变量 {rw['slow_n']} 条")
    if rw["downweight_fast"]:
        L.append("    · ⚠ 当前由框架/beta 主导，个股**快变量基本面(订单/交付/减值等)对当前股价"
                 "解释力被稀释**，交易含义里应下调其即时权重、标注'非当前主导变量'；"
                 "慢变量(护城河/客户结构)不受影响。")
        if rw["fast_samples"]:
            L.append(f"    · 受影响的快变量示例：{'、'.join(rw['fast_samples'])}")
    if snap.errors:
        L.append(f"- （部分维度取数降级：{'、'.join(snap.errors)}）")
    return "\n".join(L)

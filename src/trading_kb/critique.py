"""质疑模块:对每条结论做批判性体检(投研的"魔鬼代言人")。

三项体检(对应用户需求):
  ① 无出处/推测   —— 没有原文证据、数字没核对过、或满是推测措辞却无数据支撑
  ② 过于乐观/夸大 —— 关键数字(年化/IC/夏普等)显著高于同类(分位对照)
  ③ 回测/样本软肋 —— 回测无样本外验证、区间短、研报自陈 caveats、缺反例

设计:
- 规则核心,确定性可复现;LLM 钩子可深挖逻辑漏洞(预留)。
- ② 需先 fit 全库分布,再对单条判分位(两遍),纯本地、跑真实量化语料。
- 联网权威佐证(web_enrich)缺失时,乐观项只"存疑"不"证伪"(铁律:查不到≠假)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional

from .models import Finding

# ── 推测措辞(① 无据推测信号)──────────────────────────────────────────────
_HEDGE_WORDS = ["预计", "可能", "有望", "或将", "据传", "假设", "预期", "估计",
                "大概率", "料将", "传闻", "猜测", "应该会", "看好", "乐观"]

# ── 指标抽取(② 乐观对照):context 关键词 → 指标名 + 是否取绝对值 + 方向 ────
# 方向 +1 表示"越大越乐观";绝对值用于 IC/ICIR(负也代表强,看 |x|)
_METRIC_PATTERNS = [
    ("annual_return", ["年化"], False, +1),     # 年化收益率:越高越乐观
    ("icir", ["icir", "ic_ir", "信息系数比"], True, +1),
    ("ic", ["rankic", "rank ic", "ic均值", "ic"], True, +1),
    ("info_ratio", ["信息比"], False, +1),
    ("sharpe", ["夏普"], False, +1),
    ("win_rate", ["胜率"], False, +1),
    ("excess", ["超额"], False, +1),
]

# ── 回测软肋信号(③)────────────────────────────────────────────────────
_BACKTEST_WORDS = ["回测", "历史表现", "样本内"]
_OUT_OF_SAMPLE = ["样本外", "out of sample", "out-of-sample", "实盘", "滚动", "样本外验证"]


@dataclass
class CritiqueFlag:
    """一条质疑标记。"""
    kind: str            # no_source / speculative / over_optimistic / backtest_weak
    severity: str        # high / medium / low
    message: str

    def tag(self) -> str:
        icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(self.severity, "•")
        return f"{icon} {self.message}"


@dataclass
class CritiqueResult:
    flags: list[CritiqueFlag] = field(default_factory=list)

    @property
    def has_doubt(self) -> bool:
        return bool(self.flags)

    @property
    def max_severity(self) -> Optional[str]:
        for s in ("high", "medium", "low"):
            if any(f.severity == s for f in self.flags):
                return s
        return None


class CritiqueEngine:
    """批判性体检引擎。用法:fit(findings) → critique(finding)。"""

    def __init__(self, p_high: float = 95.0, p_med: float = 90.0):
        self.p_high = p_high
        self.p_med = p_med
        self._dist: dict[str, list[float]] = defaultdict(list)
        self._thresholds: dict[str, tuple[float, float]] = {}

    # ① 分布拟合(② 乐观判定需要全库基准)──────────────────────────────────
    def fit(self, findings: list[Finding]) -> "CritiqueEngine":
        """收集全库各指标分布,算分位阈值。"""
        for f in findings:
            for metric, val in _extract_metrics(f):
                self._dist[metric].append(val)
        try:
            import numpy as np
            for metric, vals in self._dist.items():
                if len(vals) >= 8:                      # 样本太少不做分位判定
                    arr = np.array(vals, dtype=float)
                    self._thresholds[metric] = (
                        float(np.percentile(arr, self.p_high)),
                        float(np.percentile(arr, self.p_med)),
                    )
        except ImportError:
            # 无 numpy:用排序分位兜底
            for metric, vals in self._dist.items():
                if len(vals) >= 8:
                    s = sorted(vals)
                    self._thresholds[metric] = (_pct(s, self.p_high), _pct(s, self.p_med))
        return self

    # 三项体检 ──────────────────────────────────────────────────────────────
    def critique(self, f: Finding, web=None) -> CritiqueResult:
        """对单条 finding 出质疑清单。web 为可选权威佐证钩子(web_enrich)。"""
        res = CritiqueResult()
        text = f"{f.claim} {f.evidence}"

        # ① 无出处 / 推测
        if not f.evidence and f.verified_numbers == 0 and not f.numbers:
            res.flags.append(CritiqueFlag(
                "no_source", "high", "无原文证据、无可核对数字 → 出处存疑"))
        hedges = [w for w in _HEDGE_WORDS if w in text]
        if hedges and f.verified_numbers == 0:
            res.flags.append(CritiqueFlag(
                "speculative", "medium",
                f"含推测措辞「{'/'.join(hedges[:3])}」却无核对数字 → 可能是猜测"))

        # ② 过于乐观 / 夸大(分位对照)
        for metric, val in _extract_metrics(f):
            thr = self._thresholds.get(metric)
            if not thr:
                continue
            hi, med = thr
            name = _METRIC_CN.get(metric, metric)
            if val >= hi:
                sev, pc = "high", self.p_high
            elif val >= med:
                sev, pc = "medium", self.p_med
            else:
                continue
            msg = f"{name} {val:g} 高于同类 {pc:.0f}% 分位 → 显著偏高,警惕过于乐观"
            # 有权威佐证则弱化为提示
            if web is not None and web(f, metric) == "corroborated":
                msg += "(已获权威佐证,可信度上调)"
                sev = "low"
            res.flags.append(CritiqueFlag("over_optimistic", sev, msg))

        # ③ 回测 / 样本软肋
        if any(w in text for w in _BACKTEST_WORDS) and not any(w in text.lower() for w in _OUT_OF_SAMPLE):
            res.flags.append(CritiqueFlag(
                "backtest_weak", "medium", "提及回测但未见样本外/实盘验证 → 警惕过拟合"))
        yr = _backtest_years(text)
        if yr is not None and yr < 3:
            res.flags.append(CritiqueFlag(
                "backtest_weak", "medium", f"回测区间仅约 {yr} 年,样本偏短 → 结论稳健性存疑"))

        # 去重:同一条 message 只保留一次(同指标多数字会重复触发)
        seen = set()
        uniq = []
        for fl in res.flags:
            if fl.message in seen:
                continue
            seen.add(fl.message)
            uniq.append(fl)
        res.flags = uniq
        return res


# ── 指标抽取与解析 ──────────────────────────────────────────────────────
def _extract_metrics(f: Finding) -> list[tuple[str, float]]:
    """从 finding 的 numbers(带 context)抽取 (指标名, 数值)。"""
    out = []
    for n in f.numbers or []:
        ctx = str(n.get("context", "")).lower()
        val = _parse_num(n.get("value"))
        if val is None:
            continue
        for metric, kws, use_abs, _dir in _METRIC_PATTERNS:
            if any(kw in ctx for kw in kws):
                out.append((metric, abs(val) if use_abs else val))
                break
    return out


def _parse_num(value) -> Optional[float]:
    """'40.12%' → 40.12;'-5.51' → -5.51;无数字 → None。"""
    if value is None:
        return None
    m = re.search(r"-?\d+\.?\d*", str(value))
    return float(m.group(0)) if m else None


def _backtest_years(text: str) -> Optional[float]:
    """从 '回测2013.1-2023.10' / '2013-2023' 估算区间年数。"""
    m = re.search(r"(20\d{2})[.\-/年]?\d{0,2}[^\d]{0,4}(20\d{2})", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b >= a:
            return float(b - a)
    return None


def _pct(sorted_vals: list[float], p: float) -> float:
    """无 numpy 时的简单分位。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


_METRIC_CN = {
    "annual_return": "年化收益", "icir": "ICIR", "ic": "IC",
    "info_ratio": "信息比", "sharpe": "夏普", "win_rate": "胜率", "excess": "超额收益",
}

"""report_lab 适配器:读取已建成的 cards/*.json,产出 Finding 列表(§6 复用)。

report_lab 是证据/抽取/校验前端(护城河);本系统消费它的 findings。
每条 finding 已经过 report_lab 的 extract + verify(0-token 数字校验)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from . import config
from .models import Finding

# report_lab 卡片 type → source_kind 映射
_TYPE_SOURCE_KIND = {
    "quant": "broker_research",
    "industry": "broker_research",
    "company": "broker_research",
    "macro": "broker_research",
    "minutes": "expert_meeting",     # 纪要 → 专家会(C级基线)
}


def iter_cards(cards_dir: Path | None = None) -> Iterator[dict]:
    """遍历 report_lab 卡片 JSON。"""
    cards_dir = cards_dir or config.REPORT_LAB_CARDS
    if not cards_dir.exists():
        return
    for fp in sorted(cards_dir.glob("*.json")):
        try:
            obj = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(obj, dict):          # A1:非 dict(如 list)跳过,不击溃整批
            yield obj


def card_to_findings(card: dict) -> list[Finding]:
    """把一张卡片展开成 Finding 列表(含量化 factors 视作 finding)。"""
    doc_id = card.get("id", "")
    source_kind = _TYPE_SOURCE_KIND.get(card.get("type", ""), "broker_research")
    date = card.get("date", "") or ""
    broker = card.get("broker", "") or ""
    findings: list[Finding] = []

    for f in card.get("findings") or []:
        if not isinstance(f, dict) or not f.get("claim"):
            continue
        findings.append(Finding(
            claim=f.get("claim", ""),
            evidence=f.get("evidence", "") or "",
            numbers=_clean_numbers(f.get("numbers")),
            entities=[_ename(e) for e in (f.get("entities") or [])],
            page=f.get("page"),
            confidence=f.get("confidence", "") or "",
            doc_id=doc_id, source_kind=source_kind, source_date=date, broker=broker,
        ))

    # 量化因子也作为 finding(本地语料主力,§v2.1 分流扩展)
    for fa in card.get("factors") or []:
        if not isinstance(fa, dict) or not fa.get("name"):
            continue
        perf = fa.get("performance") or {}
        ev = "; ".join(f"{k}={v}" for k, v in perf.items()) if isinstance(perf, dict) else ""
        findings.append(Finding(
            claim=f"因子「{fa.get('name')}」{fa.get('intuition','')}"[:120],
            evidence=ev or (fa.get("formula", "") or "")[:200],
            numbers=[{"value": str(v), "context": k, "page": fa.get("page")}
                     for k, v in perf.items()] if isinstance(perf, dict) else [],
            entities=[fa.get("name", "")],
            page=fa.get("page"), confidence="",
            doc_id=doc_id, source_kind=source_kind, source_date=date, broker=broker,
        ))
    return findings


def card_entities(card: dict) -> list[dict]:
    """卡片级实体列表(name/kind/code),用于实体注册。"""
    out = []
    for e in card.get("entities") or []:
        if isinstance(e, dict) and e.get("name"):
            out.append({"name": e["name"], "kind": e.get("kind") or "concept",
                        "code": e.get("code")})
    return out


def _ename(e) -> str:
    """实体可能是 str 或 dict,统一取名字。"""
    if isinstance(e, dict):
        return e.get("name", "")
    return str(e)


def _clean_numbers(nums) -> list[dict]:
    """归一 numbers 为 dict 列表。LLM 偶尔把数字返回成裸字符串(如 "1.6T")→包成 dict,
    防止下游 n.get(...) 崩(critique/verified_numbers 等)。非 str/dict 一律丢弃。"""
    out = []
    for n in nums or []:
        if isinstance(n, dict):
            out.append(n)
        elif isinstance(n, (str, int, float)):
            out.append({"value": str(n), "context": ""})
    return out

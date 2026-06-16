"""舆情轻 lane(§10-bis,继承 trading-review-wiki 的聊天记录处理)。

流程:碎片 → 实体过滤 → 轻抽取 → D级时序事实/舆情表 → 聚合信号 → 印证升级 → 隔离。
- 实体过滤先行:没提到关注标的的碎片连 LLM 都不喂(规模化,§10-bis ②)
- 默认 D 级 + unverifiable,隔离不进研报证据链
- 聚合:按 实体×时间 → 情绪曲线/传闻密度/谁反复提
- 升级闸门:被 B+ 信源印证 → promoted=True
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from .entity_registry import EntityRegistry
from .models import SentimentItem, _normalize

# 极简立场词典(规则核心;LLM 钩子可替换)
_BULLISH = ["看好", "起飞", "要涨", "利好", "突破", "加仓", "强势", "牛"]
_BEARISH = ["看空", "要跌", "利空", "减仓", "风险", "崩", "套牢", "出货"]


class SentimentLane:
    """轻舆情通道。"""

    def __init__(self, db_path: Path, registry: EntityRegistry):
        db_path = Path(db_path)          # 兼容 str 传入(M5)
        self.db_path = db_path
        self.registry = registry
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")   # A2 并发
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sentiment (
                item_id        TEXT PRIMARY KEY,
                text           TEXT,
                canonical_id   TEXT,
                stance         TEXT,
                claim          TEXT,
                timestamp      TEXT,
                source_kind    TEXT,
                evidence_level TEXT,
                unverifiable   INTEGER,
                promoted       INTEGER,
                promoted_by    TEXT          -- 被谁印证升级(§10-bis 留底溯源,N3)
            );
            CREATE INDEX IF NOT EXISTS idx_sent_cid ON sentiment(canonical_id);
            CREATE TABLE IF NOT EXISTS sentiment_raw (
                raw_id    TEXT PRIMARY KEY,
                text      TEXT,
                timestamp TEXT,
                kept      INTEGER          -- 是否命中关注标的(0=冷存留底)
            );
            """
        )
        self.conn.commit()

    def ingest_fragment(self, text: str, timestamp: str,
                        watch_terms: list[str], llm=None) -> Optional[SentimentItem]:
        """处理一条碎片。

        watch_terms:关注标的名/别名列表,用于实体过滤(命中才轻抽,否则冷存留底)。
        返回入库的 SentimentItem;未命中关注标的返回 None(已冷存)。
        """
        raw_id = _hash(text + timestamp)
        hits = _all_hits(text, watch_terms)        # C5:一条碎片可命中多个标的

        # 原文永久冷存留底(§10-bis 留底与溯源)
        self.conn.execute(
            "INSERT OR IGNORE INTO sentiment_raw(raw_id,text,timestamp,kept) VALUES(?,?,?,?)",
            (raw_id, text, timestamp, int(bool(hits))),
        )
        self.conn.commit()

        if not hits:
            return None   # 没提到关注标的 → 不喂 LLM,只冷存(规模化过滤)

        stance = _detect_stance(text) if llm is None else llm(text)
        first_item = None
        for term in hits:                          # 命中的每个标的各记一条(C5)
            cid = self.registry.resolve(term, type_="stock")
            item = SentimentItem(
                text=text, canonical_id=cid, stance=stance,
                claim=text[:60], timestamp=timestamp,
            )
            self.conn.execute(
                """INSERT OR IGNORE INTO sentiment
                   (item_id,text,canonical_id,stance,claim,timestamp,source_kind,
                    evidence_level,unverifiable,promoted)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item.item_id, item.text, cid, stance, item.claim, timestamp,
                 item.source_kind, item.evidence_level, int(item.unverifiable), 0),
            )
            if first_item is None:
                first_item = item
        self.conn.commit()
        return first_item

    def aggregate(self, canonical_id: str) -> dict:
        """聚合信号:某标的的情绪分布 + 碎片密度(§10-bis ① 聚合)。"""
        rows = self.conn.execute(
            "SELECT stance, timestamp FROM sentiment WHERE canonical_id=?", (canonical_id,)
        ).fetchall()
        stances = Counter(r["stance"] for r in rows)
        by_day = defaultdict(int)
        for r in rows:
            by_day[(r["timestamp"] or "")[:10]] += 1
        return {
            "canonical_id": canonical_id,
            "total": len(rows),
            "stance_dist": dict(stances),
            "density_by_day": dict(by_day),
            "net_sentiment": stances.get("bullish", 0) - stances.get("bearish", 0),
        }

    def promote(self, canonical_id: str, corroborating_source: str) -> int:
        """升级闸门:被 B+ 信源印证 → 该标的碎片 promoted=True(§10-bis ⑤)。

        记录 promoted_by=印证来源(N3:留底+可溯源一个不能少)。返回升级条数。
        """
        cur = self.conn.execute(
            "UPDATE sentiment SET promoted=1, promoted_by=? WHERE canonical_id=? AND promoted=0",
            (corroborating_source, canonical_id),
        )
        self.conn.commit()
        return cur.rowcount

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) c FROM sentiment").fetchone()["c"]
        raw = self.conn.execute("SELECT COUNT(*) c FROM sentiment_raw").fetchone()["c"]
        kept = self.conn.execute("SELECT COUNT(*) c FROM sentiment_raw WHERE kept=1").fetchone()["c"]
        promoted = self.conn.execute("SELECT COUNT(*) c FROM sentiment WHERE promoted=1").fetchone()["c"]
        return {"items": total, "raw_total": raw, "raw_kept": kept, "promoted": promoted}

    def close(self) -> None:
        self.conn.close()


# ── 辅助 ──────────────────────────────────────────────────────────────────
def _detect_stance(text: str) -> str:
    t = text.lower()
    b = sum(1 for k in _BULLISH if k in t)
    s = sum(1 for k in _BEARISH if k in t)
    if b > s:
        return "bullish"
    if s > b:
        return "bearish"
    return "neutral"


def _all_hits(text: str, terms: list[str]) -> list[str]:
    """返回 text 命中的全部关注标的(去重保序),C5 多标的归因。"""
    out = []
    for t in terms:
        if t and t in text and t not in out:
            out.append(t)
    return out


def _hash(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]

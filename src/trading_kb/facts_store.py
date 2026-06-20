"""时序事实层(Graphiti 等价实现,§18)。

忠实实现 Graphiti 的关键语义,生产可平替为 Graphiti MCP:
- 双时态:valid_at / invalid_at,证伪不删除(§16.1 回滚)
- 事实级去重合并:dedup_key 命中则累加来源、按最高信源升级成色、保留时间线(§11 F11)
- 状态机:active / superseded / invalidated / disputed
- supersede / contradict:新事实替代或反驳旧事实
- include_invalidated 检索:默认只返 active,审计可返历史(§10.3)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from .models import Fact, EvidenceLevel

_LEVEL_RANK = {"D": 0, "C": 1, "B": 2, "B+": 3, "A": 4}   # B+ 介于 B 与 A(外资行研报)


class FactsStore:
    """SQLite 时序事实账本。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)          # 兼容 str 传入(M5)
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")   # A2 并发
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS facts (
                fact_id        TEXT PRIMARY KEY,
                dedup_key      TEXT UNIQUE,
                subject        TEXT,
                predicate      TEXT,
                object         TEXT,
                canonical_id   TEXT,
                claim          TEXT,
                status         TEXT,
                evidence_level TEXT,
                unverifiable   INTEGER,
                source_kind    TEXT,
                support_count  INTEGER,
                sources        TEXT,
                valid_at       TEXT,
                invalid_at     TEXT,
                supersedes     TEXT,
                relation_id    TEXT,
                category       TEXT,
                extra          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_facts_cid ON facts(canonical_id);
            CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);
            CREATE INDEX IF NOT EXISTS idx_facts_pred ON facts(predicate);
            """
        )
        self.conn.commit()

    # ── 写入(含去重合并)─────────────────────────────────────────────────
    def upsert(self, fact: Fact) -> str:
        """写入事实;dedup_key 命中则合并(累加来源/升级成色/保留时间线)。

        返回 fact_id。重复执行幂等(§18 deterministic id)。
        """
        existing = self.conn.execute(
            "SELECT * FROM facts WHERE dedup_key=?", (fact.dedup_key,)
        ).fetchone()

        if existing is None:
            row = fact.to_row()
            row["unverifiable"] = int(fact.unverifiable)
            self.conn.execute(
                """INSERT INTO facts
                   (fact_id,dedup_key,subject,predicate,object,canonical_id,claim,status,
                    evidence_level,unverifiable,source_kind,support_count,sources,valid_at,
                    invalid_at,supersedes,relation_id,category,extra)
                   VALUES
                   (:fact_id,:dedup_key,:subject,:predicate,:object,:canonical_id,:claim,:status,
                    :evidence_level,:unverifiable,:source_kind,:support_count,:sources,:valid_at,
                    :invalid_at,:supersedes,:relation_id,:category,:extra)""",
                row,
            )
            self.conn.commit()
            return fact.fact_id

        # 合并:累加来源、升级成色、support_count、保留最早 valid_at
        merged_sources = sorted(set(json.loads(existing["sources"]) + fact.sources))
        best_level = _max_level(existing["evidence_level"], fact.evidence_level)
        # unverifiable 仅当"两条都未经数据验证"时才保持 True。
        # 注:unverifiable=False 只由 grade 的数据验证(confirmed/refuted)产生,
        # 故 AND 语义 = "任一来源经数据验证则整体已验证",是真实的多源印证,非洗白(M4)。
        unver = int(bool(existing["unverifiable"]) and fact.unverifiable)
        valid_at = min(filter(None, [existing["valid_at"], fact.valid_at]), default=fact.valid_at)
        # 同 key 再次出现(如同一论断被再次确认):若旧行已被 superseded/invalidated,
        # 在合并时复活为 active(C1:避免 supersede 自碰撞导致事实丢失)。
        revive = existing["status"] in ("superseded", "invalidated", "expired")
        if revive:
            self.conn.execute(
                """UPDATE facts SET sources=?, support_count=?, evidence_level=?, unverifiable=?,
                                     valid_at=?, status='active', invalid_at=NULL WHERE dedup_key=?""",
                (json.dumps(merged_sources, ensure_ascii=False), len(merged_sources),
                 best_level, unver, valid_at, fact.dedup_key),
            )
        else:
            self.conn.execute(
                """UPDATE facts SET sources=?, support_count=?, evidence_level=?, unverifiable=?,
                                     valid_at=? WHERE dedup_key=?""",
                (json.dumps(merged_sources, ensure_ascii=False), len(merged_sources),
                 best_level, unver, valid_at, fact.dedup_key),
            )
        self.conn.commit()
        return existing["fact_id"]

    # ── 状态变更 ──────────────────────────────────────────────────────────
    def supersede(self, old_fact_id: str, new_fact: Fact, at: str) -> str:
        """新事实替代旧事实:旧标 superseded + invalid_at,新事实记 supersedes。

        C1 修复:若新旧是同一事实(同 dedup_key/fact_id),不能"标旧 superseded 再 upsert"
        ——那样新事实会落到同一行并被标记消失。此时改为原地合并复活(active)。
        """
        if new_fact.fact_id == old_fact_id:
            # 同一论断再次确认:不自我替代,走 upsert 的复活+合并路径(见 upsert)
            return self.upsert(new_fact)
        self.conn.execute(
            "UPDATE facts SET status='superseded', invalid_at=? WHERE fact_id=?",
            (at, old_fact_id),
        )
        new_fact.supersedes = sorted(set(new_fact.supersedes + [old_fact_id]))
        self.conn.commit()
        nid = self.upsert(new_fact)
        # B4:upsert 合并路径不写 supersedes 列,这里显式落库替代血缘
        existing = self.conn.execute(
            "SELECT supersedes FROM facts WHERE fact_id=?", (nid,)
        ).fetchone()
        prev = json.loads(existing["supersedes"]) if existing and existing["supersedes"] else []
        merged = sorted(set(prev) | set(new_fact.supersedes))
        self.conn.execute("UPDATE facts SET supersedes=? WHERE fact_id=?",
                          (json.dumps(merged, ensure_ascii=False), nid))
        self.conn.commit()
        return nid

    def contradict(self, target_fact_id: str, at: str, by_source: str = "") -> None:
        """反证:旧事实标 invalidated + invalid_at(不删除,§16.1 回滚)。"""
        self.conn.execute(
            "UPDATE facts SET status='invalidated', invalid_at=? WHERE fact_id=?",
            (at, target_fact_id),
        )
        self.conn.commit()

    def mark_disputed(self, fact_id: str) -> None:
        """多源冲突:标 disputed(等后续权威 supersede)。"""
        self.conn.execute("UPDATE facts SET status='disputed' WHERE fact_id=?", (fact_id,))
        self.conn.commit()

    def expire_before(self, cutoff_valid_at: str) -> int:
        """把 valid_at 早于 cutoff 的 active 事实标 expired(N2:时间窗口过期,不删除)。

        供审计/定期维护调用;默认管线不自动过期。返回过期条数。
        """
        cur = self.conn.execute(
            "UPDATE facts SET status='expired' WHERE status='active' "
            "AND valid_at!='' AND valid_at < ?", (cutoff_valid_at,),
        )
        self.conn.commit()
        return cur.rowcount

    # ── 检索 ──────────────────────────────────────────────────────────────
    def query(self, canonical_id: Optional[str] = None, predicate: Optional[str] = None,
              include_invalidated: bool = False, limit: int = 100) -> list[dict]:
        """检索事实。默认只返 active/disputed;include_invalidated=True 返历史(§10.3 审计)。"""
        sql = "SELECT * FROM facts WHERE 1=1"
        args: list = []
        if canonical_id:
            sql += " AND canonical_id=?"
            args.append(canonical_id)
        if predicate:
            sql += " AND predicate=?"
            args.append(predicate)
        if not include_invalidated:
            sql += " AND status IN ('active','disputed')"
        sql += " ORDER BY evidence_level DESC, support_count DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def get(self, fact_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM facts GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["c"] for r in rows}
        levels = self.conn.execute(
            "SELECT evidence_level, COUNT(*) c FROM facts GROUP BY evidence_level"
        ).fetchall()
        by_level = {r["evidence_level"]: r["c"] for r in levels}
        total = self.conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
        return {"total": total, "by_status": by_status, "by_level": by_level}

    def close(self) -> None:
        self.conn.close()


def _max_level(a: EvidenceLevel, b: EvidenceLevel) -> EvidenceLevel:
    """取更高成色。"""
    return a if _LEVEL_RANK.get(a, 0) >= _LEVEL_RANK.get(b, 0) else b

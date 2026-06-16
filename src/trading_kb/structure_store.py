"""结构关系层(LightRAG typed 产业链图的等价实现,§18)。

生产可平替为 LightRAG。此处实现 typed 边 + 多篇投票(§18 F6):
- 边带 support_count + sources[],单篇孤证标 low_confidence
- 邻居查询支撑"拆解行业"(给实体 → 上下游环节)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Relation


class StructureStore:
    """SQLite typed 关系图。"""

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
            CREATE TABLE IF NOT EXISTS relations (
                rel_id        TEXT PRIMARY KEY,
                src           TEXT,
                rel_type      TEXT,
                dst           TEXT,
                support_count INTEGER,
                sources       TEXT,
                low_confidence INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(src);
            CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(dst);
            """
        )
        self.conn.commit()

    def upsert(self, rel: Relation) -> str:
        """写入关系;命中则多篇投票累加 support_count,达标后摘掉 low_confidence。"""
        existing = self.conn.execute(
            "SELECT * FROM relations WHERE rel_id=?", (rel.rel_id,)
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """INSERT INTO relations(rel_id,src,rel_type,dst,support_count,sources,low_confidence)
                   VALUES(?,?,?,?,?,?,?)""",
                (rel.rel_id, rel.src, rel.rel_type, rel.dst, len(set(rel.sources)) or 1,
                 json.dumps(sorted(set(rel.sources)), ensure_ascii=False),
                 int(len(set(rel.sources)) < 2)),
            )
        else:
            merged = sorted(set(json.loads(existing["sources"]) + rel.sources))
            self.conn.execute(
                "UPDATE relations SET support_count=?, sources=?, low_confidence=? WHERE rel_id=?",
                (len(merged), json.dumps(merged, ensure_ascii=False),
                 int(len(merged) < 2), rel.rel_id),
            )
        self.conn.commit()
        return rel.rel_id

    def neighbors(self, node: str, rel_type: str | None = None) -> list[dict]:
        """查某实体的产业链邻居(双向)。拆解行业用。"""
        sql = "SELECT * FROM relations WHERE (src=? OR dst=?)"
        args: list = [node, node]
        if rel_type:
            sql += " AND rel_type=?"
            args.append(rel_type)
        sql += " ORDER BY support_count DESC"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
        low = self.conn.execute(
            "SELECT COUNT(*) c FROM relations WHERE low_confidence=1"
        ).fetchone()["c"]
        by_type = {r["rel_type"]: r["c"] for r in self.conn.execute(
            "SELECT rel_type, COUNT(*) c FROM relations GROUP BY rel_type").fetchall()}
        return {"total": total, "low_confidence": low, "by_type": by_type}

    def close(self) -> None:
        self.conn.close()

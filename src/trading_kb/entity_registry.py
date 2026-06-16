"""实体注册表:把实体名归一到 canonical_id(§17 命门)。

- 股票  → 证券代码(SH/SZ + 6位),优先用 report_lab 卡片已带的 code,其次内置表/tdx。
- 概念/材料 → controlled:<归一名>(无证券代码,用受控前缀)。
- 支持别名与事后合并(merged_into),避免碎片永久存在(§17 F5)。

设计取舍:tdx 在线代码表是可选增强;离线时用卡片内 code + 别名归一,保证可复现。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .models import _normalize


class EntityRegistry:
    """证券代码/概念归一注册表,三层(facts/structure/sentiment)共享主键。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)          # 兼容 str 传入(M5)
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")   # A2:并发等锁而非立即崩
        self._init_schema()

    def _init_schema(self) -> None:
        """建表:实体主表 + 别名表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
                canonical_id TEXT PRIMARY KEY,
                display_name TEXT,
                type         TEXT,          -- stock/concept/material/company/person/index
                stock_code   TEXT,
                board_source TEXT,
                merged_into  TEXT,          -- 指向合并目标(去碎片)
                source       TEXT
            );
            CREATE TABLE IF NOT EXISTS aliases (
                alias_norm   TEXT PRIMARY KEY,
                canonical_id TEXT
            );
            """
        )
        self.conn.commit()

    # ── 写入 ──────────────────────────────────────────────────────────────
    def register(self, name: str, type_: str = "concept",
                 stock_code: Optional[str] = None, source: str = "ingest") -> str:
        """登记一个实体,返回 canonical_id。重复登记幂等。"""
        cid = self._canonical_id(name, type_, stock_code)
        # A2:INSERT OR IGNORE 消除 SELECT-then-INSERT 的并发竞态
        self.conn.execute(
            "INSERT OR IGNORE INTO entities(canonical_id,display_name,type,stock_code,source) "
            "VALUES(?,?,?,?,?)",
            (cid, name, type_, stock_code, source),
        )
        # 别名(归一名)指向 cid
        self.conn.execute(
            "INSERT OR IGNORE INTO aliases(alias_norm,canonical_id) VALUES(?,?)",
            (_normalize(name), cid),
        )
        self.conn.commit()
        return cid

    def add_alias(self, alias: str, canonical_id: str) -> None:
        """为已有实体加别名。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO aliases(alias_norm,canonical_id) VALUES(?,?)",
            (_normalize(alias), canonical_id),
        )
        self.conn.commit()

    def merge(self, from_id: str, into_id: str) -> None:
        """事后合并:from_id 标记 merged_into into_id,别名改指 into_id(§17 F5)。"""
        self.conn.execute("UPDATE entities SET merged_into=? WHERE canonical_id=?", (into_id, from_id))
        self.conn.execute("UPDATE aliases SET canonical_id=? WHERE canonical_id=?", (into_id, from_id))
        self.conn.commit()

    # ── 解析 ──────────────────────────────────────────────────────────────
    def resolve(self, name: str, type_: str = "concept",
                stock_code: Optional[str] = None) -> str:
        """把名字解析到 canonical_id;未登记则自动登记(§9 [8])。

        跟随 merged_into 链,保证拿到合并后的最终主键。
        """
        row = self.conn.execute(
            "SELECT canonical_id FROM aliases WHERE alias_norm=?", (_normalize(name),)
        ).fetchone()
        cid = row["canonical_id"] if row else self.register(name, type_, stock_code)
        return self._follow_merge(cid)

    def _follow_merge(self, cid: str) -> str:
        """跟随 merged_into 链到终点。"""
        seen = set()
        while cid and cid not in seen:
            seen.add(cid)
            row = self.conn.execute(
                "SELECT merged_into FROM entities WHERE canonical_id=?", (cid,)
            ).fetchone()
            if row and row["merged_into"]:
                cid = row["merged_into"]
            else:
                break
        return cid

    @staticmethod
    def _canonical_id(name: str, type_: str, stock_code: Optional[str]) -> str:
        """生成 canonical_id(A4 修正)。

        - 股票 + code → 证券代码;股票无 code → stock_pending。
        - 非股票:用 type 前缀(index:/fund:/product:/concept:...);
          但带 .OF 场外基金代码的(常被标 product/fund)→ fund:代码,避免错挂股票。
        """
        if type_ == "stock":
            return _to_market_code(stock_code) if stock_code else f"stock_pending:{_normalize(name)}"
        # 非股票:仅 .OF 场外基金代码走代码路由(→ fund:digits)
        if stock_code and ".OF" in stock_code.upper():
            return _to_market_code(stock_code)
        return f"{type_}:{_normalize(name)}"

    def watch_terms(self, types: tuple = ("stock",)) -> list[str]:
        """返回可作"关注标的"的实体显示名(默认仅股票),供舆情 lane 实体过滤。

        舆情碎片只有命中关注标的才轻抽入库,否则冷存留底(§10-bis ②)。
        已合并(merged_into)的实体跳过,避免用废名做过滤。
        """
        qmarks = ",".join("?" * len(types))
        rows = self.conn.execute(
            f"SELECT DISTINCT display_name FROM entities "
            f"WHERE type IN ({qmarks}) AND (merged_into IS NULL OR merged_into='') "
            f"AND display_name IS NOT NULL AND display_name<>''",
            tuple(types),
        ).fetchall()
        return sorted(r["display_name"] for r in rows)

    def stats(self) -> dict:
        """注册表规模统计。"""
        n = self.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        pending = self.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE canonical_id LIKE 'stock_pending:%'"
        ).fetchone()["c"]
        return {"entities": n, "pending_stocks": pending}

    def close(self) -> None:
        self.conn.close()


def _to_market_code(code: str) -> str:
    """把 6 位代码补市场前缀(M1 修正)。已带前缀则归一为大写。

    规则:
      6xxxxx → SH(沪主板/科创)   0xx/3xx → SZ(深主板/创业)
      900xxx → SH(沪B)           200xxx → SZ(深B)
      920xxx → BJ(北交所)        8xxxxx/4xxxxx → BJ(北交所/老三板)
      其余未知 → stock_pending(不静默错挂)
    """
    code = code.strip().upper()
    # 场外基金后缀(.OF/.OFCN 等)→ 基金,不当股票(A4)
    if ".OF" in code:
        d = "".join(c for c in code if c.isdigit())
        return f"fund:{d}" if d else f"fund:{code.lower()}"
    if code[:2] in ("SH", "SZ", "BJ") and code[2:].isdigit():
        return code
    d = "".join(c for c in code if c.isdigit())
    if len(d) != 6:
        return f"stock_pending:{code.lower()}"
    if d[:3] == "920":
        return f"BJ{d}"            # 北交所 920 段(须先于 9 开头判断)
    if d[0] == "6":
        return f"SH{d}"
    if d[0] in ("0", "3"):
        return f"SZ{d}"
    if d[0] == "5":
        return f"SH{d}"            # 沪市 ETF/基金(50/51/56/58)
    if d[:2] in ("15", "16", "18"):
        return f"SZ{d}"            # 深市 ETF(15/16/18)
    if d[0] == "9":
        return f"SH{d}"            # 沪 B 股 900
    if d[0] == "2":
        return f"SZ{d}"            # 深 B 股 200
    if d[0] in ("8", "4"):
        return f"BJ{d}"            # 北交所/老三板
    return f"stock_pending:{d}"    # 未知段不静默错挂

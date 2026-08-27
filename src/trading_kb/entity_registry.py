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

from .entity_quality import is_garbage_entity, is_pseudo_company
from .models import Relation, _normalize

# 垃圾实体(日期/纯数值/法条/地域/通用词)登记时一律归到此主键,不污染注册表
UNKNOWN_CID = "concept:未知主体"


class EntityRegistry:
    """证券代码/概念归一注册表,三层(facts/structure/sentiment)共享主键。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)          # 兼容 str 传入(M5)
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")   # 读写不互斥;备份须走 sqlite3 .backup(ARCHITECTURE.md §2.4)   # A2:并发等锁而非立即崩
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
        """登记一个实体,返回 canonical_id。重复登记幂等。

        垃圾实体闸(治本):无股票代码且名字是日期/纯数值/法条/地域市场/通用词时,
        不登记、直接归到 UNKNOWN_CID(未知主体),从源头杜绝注册表污染。
        """
        if not stock_code and is_garbage_entity(name, type_):
            return UNKNOWN_CID
        # 伪公司闸门(2026-08-27 下沉到此,单一定义点):LLM 把"北美AI集群/HBM先进封装"这类主题短语
        # 标成 company → 降为 concept 登记。此前只在 ingest.ingest_card 里判,kb_adapter 手抄的
        # 实体循环、_ingest_cross_market、llm_attribute_unknown 都绕过了它,retype 两天后 08-27
        # 又再生 91 个。带 stock_code 的路径不受影响(有代码就是真证券)。
        if type_ == "company" and not stock_code and is_pseudo_company(name):
            type_ = "concept"
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

    # ── 公共查询(供 ask 等上层用;conn 是私有实现,上层不得裸摸)────────────
    def aliases_of(self, canonical_id: str) -> set[str]:
        """某实体的全部非空别名(归一形)。"""
        return {r["alias_norm"] for r in self.conn.execute(
            "SELECT alias_norm FROM aliases WHERE canonical_id=?", (canonical_id,)
        ).fetchall() if r["alias_norm"]}

    def get(self, canonical_id: str) -> Optional[dict]:
        """按 canonical_id 取实体主表一行(dict);不存在返回 None。不跟随 merged_into。"""
        row = self.conn.execute(
            "SELECT * FROM entities WHERE canonical_id=?", (canonical_id,)).fetchone()
        return dict(row) if row else None

    def iter_aliases(self) -> list:
        """全部 (alias_norm, canonical_id),按别名长度降序(实体定位用)。"""
        return self.conn.execute(
            "SELECT alias_norm, canonical_id FROM aliases ORDER BY LENGTH(alias_norm) DESC"
        ).fetchall()

    def aliases_with_length(self, n: int) -> list:
        """指定长度的 (alias_norm, canonical_id)(字形纠错用)。"""
        return self.conn.execute(
            "SELECT alias_norm, canonical_id FROM aliases WHERE LENGTH(alias_norm)=?", (n,)
        ).fetchall()

    def merge(self, from_id: str, into_id: str, sync_relations: bool = True) -> None:
        """事后合并:from_id 标记 merged_into into_id,别名改指 into_id(§17 F5)。

        边界警示:本方法只改注册表,**不改 facts 表的 canonical_id**——事实侧的
        重挂由治理脚本(scripts/clean_entities._reattribute)完成,两步不是原子的。
        两条 UPDATE 共享 python sqlite3 的隐式事务,commit 一次原子生效。

        但 structure.relations 的 src/dst **必须在这里同步改指**(sync_relations=True):
        关系边不改指的后果不是"噪声"而是【静默丢失】——边上还写着旧 cid,
        `structure.neighbors(新 cid)` 查不到它,而旧 cid 已被 resolve() 解析成新 cid
        也查不到,两头都够不着。2026-08-25 曾一次性修过 8,413 条(fix_relation_merged_refs.py),
        但那是手工补丁没堵住入口,一天多后又攒了 47 条。闸口放在这里才覆盖全部
        调用方(merge_fragments / merge_typo_fragments / merge_concept_companies /
        merge_english_fragments,以及将来任何新增的合并脚本)。
        """
        # 护栏(审核 A P2-18):自合并与成环都会让 _follow_merge 把该实体"合并到自己/对方",
        # watch_terms 不再含该股、resolve 结果漂移。出声跳过而非抛错(调用方多是批处理脚本)。
        if from_id == into_id or self._follow_merge(into_id) == from_id:
            import sys as _sys
            print(f"⚠ merge({from_id} → {into_id}) 跳过:自合并或会形成合并环", file=_sys.stderr)
            return
        self.conn.execute("UPDATE entities SET merged_into=? WHERE canonical_id=?", (into_id, from_id))
        self.conn.execute("UPDATE aliases SET canonical_id=? WHERE canonical_id=?", (into_id, from_id))
        self.conn.commit()
        if sync_relations:
            self._sync_relations(from_id, into_id)

    def _sync_relations(self, from_id: str, into_id: str) -> int:
        """把 structure.relations 里指向 from_id 的边改指到 into_id(的合并终点)。返回改动条数。

        跨库(entities.db / structure.db)无法与上面的 UPDATE 同事务;这里的取舍是
        **失败不阻断 merge**——注册表已提交,关系没改指最坏退回到"改前状态"(即历史现状),
        而抛异常会让调用方以为整个 merge 失败、重跑时注册表已是合并态,更难收拾。

        rel_id 与 models.Relation.rel_id 同口径(sha1(归一src|type|归一dst)[:16]),
        改指后必须重算:碰上已存在的同 id 边则并入(sources 取并集、support_count 跟随),
        改指后 src==dst 的自环直接删。

        ⚠ from_id 端**直接映射到 into_id**,不走 _follow_merge 查表:上面那条
        `UPDATE entities … WHERE canonical_id=from_id` 在 from_id 未登记时影响 0 行,
        此时 _follow_merge(from_id) 原样返回 from_id,改指会静默失效。合并的目标由
        调用方给定,不该反过来依赖注册表是否已有该行。另一端仍走 _follow_merge
        (它可能早被合并过,要跟到终点)。
        """
        import hashlib
        import json

        # 三个库同目录(见 config:FACTS_DB/STRUCTURE_DB/ENTITY_DB 共用 DATA_DIR)。
        # 从自身路径推导而非 import config,测试传临时 db_path 时也能对上。
        structure_path = self.db_path.parent / "structure.db"
        if not structure_path.exists():
            return 0                      # 没有结构层(如纯注册表测试)→ no-op
        sc = None
        try:
            sc = sqlite3.connect(str(structure_path), timeout=30)
            sc.row_factory = sqlite3.Row
            sc.execute("PRAGMA busy_timeout=30000")   # kbsync 可能正在写 structure.db
            rows = sc.execute(
                "SELECT * FROM relations WHERE src=? OR dst=?", (from_id, from_id)
            ).fetchall()
            if not rows:
                return 0                  # 绝大多数 merge 走这里:两个索引点查,成本可忽略
            target = self._follow_merge(into_id)

            def _to(cid: str) -> str:
                return target if cid == from_id else self._follow_merge(cid)

            changed = 0
            for r in rows:
                src, dst = _to(r["src"]), _to(r["dst"])
                if src == dst:                                   # 合并后自环 → 删
                    sc.execute("DELETE FROM relations WHERE rel_id=?", (r["rel_id"],))
                    changed += 1
                    continue
                # rel_id 口径唯一定义点 = models.Relation.rel_id,不手抄哈希(§2.3)
                rid = Relation(src=src, rel_type=r["rel_type"], dst=dst).rel_id
                if rid == r["rel_id"]:
                    sc.execute("UPDATE relations SET src=?, dst=? WHERE rel_id=?", (src, dst, rid))
                elif sc.execute("SELECT 1 FROM relations WHERE rel_id=?", (rid,)).fetchone():
                    ex = sc.execute("SELECT sources FROM relations WHERE rel_id=?", (rid,)).fetchone()
                    srcs = sorted(set(json.loads(ex["sources"] or "[]")
                                      + json.loads(r["sources"] or "[]")))
                    # low_confidence 与 StructureStore.upsert 同口径(孤证=1):并入后多源须重算
                    sc.execute("UPDATE relations SET sources=?, support_count=?, low_confidence=? "
                               "WHERE rel_id=?",
                               (json.dumps(srcs, ensure_ascii=False), len(srcs) or 1,
                                int(len(srcs) < 2), rid))
                    sc.execute("DELETE FROM relations WHERE rel_id=?", (r["rel_id"],))
                else:
                    sc.execute("UPDATE relations SET rel_id=?, src=?, dst=? WHERE rel_id=?",
                               (rid, src, dst, r["rel_id"]))
                changed += 1
            sc.commit()
            return changed
        except (sqlite3.Error, ValueError) as exc:      # ValueError:sources 列非法 JSON
            import sys as _sys
            print(f"⚠ merge({from_id}) 关系改指失败(注册表已提交,关系维持原状): {exc}", file=_sys.stderr)
            return 0
        finally:
            if sc is not None:
                sc.close()

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

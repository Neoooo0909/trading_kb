"""时序事实层(Graphiti 等价实现,§18)。

忠实实现 Graphiti 的关键语义,生产可平替为 Graphiti MCP:
- 双时态:valid_at / invalid_at,证伪不删除(§16.1 回滚)
- 事实级去重合并:dedup_key 命中则累加来源、按最高信源升级成色、保留时间线(§11 F11)
- 状态机:active / superseded / invalidated / disputed
- supersede / contradict:新事实替代或反驳旧事实
- include_invalidated 检索:默认只返 active,审计可返历史(§10.3)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from .dates import clean_date
from .models import Fact, EvidenceLevel, LEVEL_RANK, content_grams, _normalize

_WARNED: set = set()      # 进程内只出声一次的降级告警(§2.2 出声,但别刷屏)

_LEVEL_RANK = LEVEL_RANK   # 唯一定义点在 models.LEVEL_RANK;此别名兼容既有导入

# search 的停用 gram:出现在极大比例事实里的 2-gram(公司名后缀/公告套词/泛化行业词),
# 作为 LIKE 条件没有区分度、只会把 LIMIT 名额吃光。仅过滤自动切出的 gram,不影响完整词。
_STOP_GRAMS = {"股份", "公司", "有限", "集团", "科技", "关于", "公告", "市场",
               "行业", "中国", "股东", "计划", "发展", "控股"}


_CODE_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")


def _is_code(cid) -> bool:
    """canonical_id 是否证券代码主体(SH/SZ/BJ + 6 位)。"""
    return bool(cid) and bool(_CODE_RE.match(str(cid)))


# 库 schema 版本(PRAGMA user_version,2026-08-27 起):1 = doc_claim 二元主键(08-27 上午);
# 2 = doc_claim 三元主键 (doc_id, ckey, fact_id) + 部分表达式索引 idx_facts_doubt。
# 迁移只在 `FactsStore.migrate()`(./tkb migrate / docclaim build)显式执行,绝不在打开库时做——
# 建索引/重建表在 190 万行库上要几十秒,并发进程在构造函数里等锁超时会整进程死。
SCHEMA_VERSION = 2
DOUBT_INDEX_SQL = ("CREATE INDEX IF NOT EXISTS idx_facts_doubt ON facts("
                   "json_extract(extra,'$.doubt_severity')) "
                   "WHERE json_extract(extra,'$.doubt_severity') IS NOT NULL")


def extra_of(row) -> dict:
    """事实行的 extra JSON → dict。缺失/畸形/合法 JSON 但非对象(如 `[1,2]`)一律 {}。

    单一定义点(2026-08-27):此前 ask/critique/deep_verify/web/dedup 各写一套解析,其中两套
    对 `[1,2]` 会 AttributeError——一行坏数据让整次 ask 崩、web 500。"""
    try:
        raw = row["extra"] if not isinstance(row, dict) else row.get("extra")
    except (KeyError, IndexError, TypeError):
        raw = None
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _jlist(s) -> list:
    try:
        v = json.loads(s or "[]")
        return list(v) if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def merge_fact_rows(keeper, losers) -> dict:
    """把若干 loser 行并入 keeper 行,返回 keeper 合并后的字段(纯函数,零 I/O)。

    **唯一合并口径**(2026-08-27 从 scripts/dedup_same_claim.merge_rows 上提):
    sources ∪ / support=len / evidence_level 取高 / valid_at 取非空最早 / unverifiable AND /
    supersedes ∪ / extra.entities ∪(保序) / extra.doubts ∪(按 JSON 去重) / verified_numbers max /
    extra.merged_from 累积。dedup_same_claim、requalify_quant._merge_into、clean_entities._reattribute
    三处共用;此前三处各一份手抄且 _reattribute 根本不合并(纯 DELETE 丢 sources/成色)。
    upsert 的合并路径前五项与本函数一致(有不变量测试钉住),extra 不动是有意的(首源口径优先)。
    """
    srcs = set(_jlist(keeper["sources"]))
    level = keeper["evidence_level"]
    valid_at = keeper["valid_at"] or ""
    unver = bool(keeper["unverifiable"])
    sup = set(_jlist(keeper["supersedes"]))
    ex = extra_of(keeper)
    ents = [e for e in (ex.get("entities") or []) if isinstance(e, str)]
    doubts = list(ex.get("doubts") or [])
    seen_doubt = {json.dumps(d, sort_keys=True, ensure_ascii=False) for d in doubts}
    vn = int(ex.get("verified_numbers") or 0)
    merged_from = set(ex.get("merged_from") or [])
    for l in losers:
        srcs |= set(_jlist(l["sources"]))
        if LEVEL_RANK.get(l["evidence_level"], 0) > LEVEL_RANK.get(level, 0):
            level = l["evidence_level"]
        if l["valid_at"] and (not valid_at or l["valid_at"] < valid_at):
            valid_at = l["valid_at"]
        unver = unver and bool(l["unverifiable"])
        sup |= set(_jlist(l["supersedes"]))
        lex = extra_of(l)
        for e in (lex.get("entities") or []):
            if isinstance(e, str) and e not in ents:
                ents.append(e)
        for d in (lex.get("doubts") or []):
            k = json.dumps(d, sort_keys=True, ensure_ascii=False)
            if k not in seen_doubt:
                seen_doubt.add(k)
                doubts.append(d)
        vn = max(vn, int(lex.get("verified_numbers") or 0))
        merged_from.add(l["fact_id"])
        merged_from |= set(lex.get("merged_from") or [])
    ex["entities"] = ents
    ex["doubts"] = doubts
    ex["verified_numbers"] = vn
    ex["merged_from"] = sorted(merged_from)
    return {"sources": sorted(srcs), "support_count": max(len(srcs), 1), "evidence_level": level,
            "valid_at": valid_at, "unverifiable": int(unver), "supersedes": sorted(sup), "extra": ex}


def ensure_merged_archive(conn) -> None:
    """合并归档表(可回滚):loser 整行 + keeper 合并前整行以 JSON 落 facts_merged_archive。
    DDL 唯一定义点(dedup_same_claim / _reattribute 共用)。不 commit,由调用方事务控制。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS facts_merged_archive (
             id INTEGER PRIMARY KEY, fact_id TEXT, keeper_id TEXT, role TEXT,
             row_json TEXT, merged_at TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fma_keeper ON facts_merged_archive(keeper_id)")


def archive_fact_row(conn, row, keeper_id: str, role: str, now: str) -> None:
    """把一行 facts(sqlite3.Row 或 dict)按 role(loser / keeper_before)归档。"""
    conn.execute("INSERT INTO facts_merged_archive(fact_id,keeper_id,role,row_json,merged_at) "
                 "VALUES(?,?,?,?,?)",
                 (row["fact_id"], keeper_id, role, json.dumps(dict(row), ensure_ascii=False), now))


class FactsStore:
    """SQLite 时序事实账本。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)          # 兼容 str 传入(M5)
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")   # A2 并发
        # WAL:读写不互斥(每日 cron + web 常驻 + 手动 CLI 三类进程并发碰库)。
        # 注意 WAL 库禁止裸 cp 备份,一律 sqlite3 .backup(ARCHITECTURE.md §2.4)。
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._fts_ok = False
        self._init_schema()
        self._migrate_ingested_at()
        self._init_doc_claim()
        self._init_fts()
        self.last_upsert_dup = False   # 上一次 upsert 是否被 (doc, claim) 判重拦下(供入库回执计数)
        self.last_search_mode = "none"  # 上一次 search 走的路径:fts / like(降级)——ask 据此出声

    # ── (doc_id, 归一 claim) 判重索引(2026-08-27,BACKGROUND_FIX_PLAN §6.2)────────
    # 同一来源文档里同一句论断只能有一行。fact_id 随主体归一/规则演进而变,只按 dedup_key 判重
    # 会让"文档再入库"(互动问答回补窗口、卡片改写后重入、重抽取)把已合并的重复慢慢造回来
    # (08-26 dedup 后一夜回涨 48 行)。ckey = blake2b(归一 claim) 8 字节整数,省空间。
    @staticmethod
    def claim_key(claim: str) -> int:
        """归一 claim → 64 位有符号整数键(blake2b 8 字节)。
        注意:scripts/backfill_background._ckey 是另一口径(把 doc_id 一起哈希、无符号),只用于
        该脚本内存索引,与本表互不交换数据;别拿它来 remap 本表。"""
        h = hashlib.blake2b(_normalize(claim or "").encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(h, "big", signed=True)

    def _init_doc_claim(self) -> None:
        """新库直接建三元主键版(v2);旧库(二元主键 v1)只建索引,主键迁移交 migrate()。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS doc_claim (
                doc_id  TEXT NOT NULL,
                ckey    INTEGER NOT NULL,
                fact_id TEXT NOT NULL,
                PRIMARY KEY (doc_id, ckey, fact_id)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS idx_doc_claim_fid ON doc_claim(fact_id);
            """
        )

    def _doc_claim_pk_arity(self) -> int:
        """doc_claim 主键列数:2 = 旧版(例外行登记不上),3 = 新版。"""
        return sum(1 for r in self.conn.execute("PRAGMA table_info(doc_claim)") if r[5])

    def doc_claim_register(self, fact_id: str, sources, claim: str) -> None:
        """登记 (doc, claim) → fact_id;INSERT OR IGNORE(同键幂等)。三元主键下同一 (doc, claim)
        可登记多个 fact_id(双证券刻意拆分行);二元主键(未迁移)下第二只证券的行登记不上——迁移后自愈。"""
        ck = self.claim_key(claim)
        self.conn.executemany("INSERT OR IGNORE INTO doc_claim(doc_id, ckey, fact_id) VALUES(?,?,?)",
                              [(d, ck, fact_id) for d in (sources or []) if d])

    def doc_claim_find_all(self, sources, claim: str) -> list:
        """任一来源文档下同句已登记且 facts 中存在的**全部**行(任意状态;JOIN 天然忽略悬空登记)。"""
        ck = self.claim_key(claim)
        out, seen = [], set()
        for d in (sources or []):
            if not d:
                continue
            for r in self.conn.execute(
                    "SELECT f.* FROM doc_claim c JOIN facts f ON f.fact_id = c.fact_id "
                    "WHERE c.doc_id=? AND c.ckey=?", (d, ck)):
                if r["fact_id"] not in seen:
                    seen.add(r["fact_id"])
                    out.append(r)
        return out

    def find_doc_claim_dup(self, sources, claim: str, canonical_id):
        """(doc, claim) 判重的**唯一判定点**(upsert 内部与 ingest 预检同调):

        同一来源文档同一句已有行 → 视为同一事实,返回那一行(任意状态,superseded 也算存在、不复活);
        例外:已登记行与新事实**都是证券码且互不相同**(同句刻意拆到两只股票)→ 不算重复。
        多行命中时优先返回 active/disputed 行(多条 active 时取任意一条),否则首行。无命中 → None。"""
        rows = self.doc_claim_find_all(sources, claim)
        if not rows:
            return None
        blocking = [r for r in rows
                    if not (_is_code(r["canonical_id"]) and _is_code(canonical_id)
                            and r["canonical_id"] != canonical_id)]
        if not blocking:
            return None
        for r in blocking:
            if r["status"] in ("active", "disputed"):
                return r
        return blocking[0]

    def doc_claim_find(self, sources, claim: str):
        """兼容旧签名:返回同句首个已存在行(不含例外判定)。新代码请用 find_doc_claim_dup。"""
        rows = self.doc_claim_find_all(sources, claim)
        return rows[0] if rows else None

    @staticmethod
    def doc_claim_remap_conn(conn, old_fact_id: str, new_fact_id: str) -> int:
        """fact_id 改写/合并时同步改指——**唯一 remap 实现**(clean_entities._reattribute、
        requalify_quant、dedup_same_claim 通过它改,不再各抄一份 UPDATE)。

        必须 `UPDATE OR REPLACE`:三元主键下 keeper 与 loser 常常同 (doc, ckey) 双登记
        (dedup 合并的每一组都是同文同句),裸 UPDATE 会撞唯一约束让整跑中止;OR REPLACE 在
        WITHOUT ROWID 表上删掉冲突的旧行,效果正是"并入 keeper"。无该表(旧库/测试库)静默跳过。"""
        try:
            return conn.execute("UPDATE OR REPLACE doc_claim SET fact_id=? WHERE fact_id=?",
                                (new_fact_id, old_fact_id)).rowcount
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                return 0
            raise

    def doc_claim_remap(self, old_fact_id: str, new_fact_id: str) -> int:
        return self.doc_claim_remap_conn(self.conn, old_fact_id, new_fact_id)

    def doc_claim_clean_dangling(self) -> int:
        """删掉指向已不存在事实的登记(被绕过 FactsStore 的脚本删行且未 remap 留下的)。
        二元主键时代它们会占着主键让新登记被 IGNORE(判重空洞);三元主键下只是垃圾,但仍清。"""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM doc_claim WHERE NOT EXISTS "
                "(SELECT 1 FROM facts f WHERE f.fact_id = doc_claim.fact_id)")
        return cur.rowcount

    # ── schema 迁移(显式执行,./tkb migrate)────────────────────────────────
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def has_doubt_index(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_facts_doubt'"
        ).fetchone() is not None

    def _write_retry(self, fn, what: str, attempts: int = 12):
        """写锁冲突退避重试(与 _doc_claim_flush 同模式):日更 ingest 可能持锁超过 busy_timeout。"""
        import time as _time
        for attempt in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == attempts - 1:
                    raise
                self.conn.rollback()
                print(f"[facts] {what}:库被占用,{5 * (attempt + 1)}s 后重试", file=sys.stderr)
                _time.sleep(5 * (attempt + 1))

    def migrate(self) -> dict:
        """把库升到 SCHEMA_VERSION。幂等;每步独立事务并出声。返回各步动作。

        v1→v2:① doc_claim 主键 (doc_id,ckey) → (doc_id,ckey,fact_id)(重建表,1.98M 行数十秒);
        ② 部分表达式索引 idx_facts_doubt(critique/deep-check 取带质疑行用;须全表 extra 皆为
        合法 JSON,否则建索引本身抛 malformed JSON——先校验,不满足只出声不建,查询侧降级全扫);
        ③ PRAGMA user_version=2。绝不在 __init__ 里做(见 SCHEMA_VERSION 注释)。"""
        out = {"from": self.schema_version(), "doc_claim_pk": "ok", "doubt_index": "ok"}
        if self._doc_claim_pk_arity() < 3:
            def _rebuild():
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.executescript(
                    """
                    CREATE TABLE doc_claim_v2 (
                        doc_id  TEXT NOT NULL, ckey INTEGER NOT NULL, fact_id TEXT NOT NULL,
                        PRIMARY KEY (doc_id, ckey, fact_id)) WITHOUT ROWID;
                    INSERT OR IGNORE INTO doc_claim_v2(doc_id, ckey, fact_id)
                        SELECT doc_id, ckey, fact_id FROM doc_claim;
                    DROP TABLE doc_claim;
                    ALTER TABLE doc_claim_v2 RENAME TO doc_claim;
                    CREATE INDEX IF NOT EXISTS idx_doc_claim_fid ON doc_claim(fact_id);
                    """)
                self.conn.commit()
            self._write_retry(_rebuild, "doc_claim 主键迁移")
            out["doc_claim_pk"] = "migrated"
        if not self.has_doubt_index():
            bad = int(self.conn.execute(
                "SELECT COUNT(*) FROM facts WHERE json_valid(extra)=0").fetchone()[0])
            if bad:
                out["doubt_index"] = f"skipped: {bad} 行 extra 非法 JSON,先修数据再建索引"
                print(f"[facts] {out['doubt_index']}", file=sys.stderr)
            else:
                try:
                    self._write_retry(lambda: (self.conn.execute(DOUBT_INDEX_SQL), self.conn.commit()),
                                      "idx_facts_doubt 建索引")
                    out["doubt_index"] = "created"
                except sqlite3.Error as e:
                    self.conn.rollback()
                    out["doubt_index"] = f"failed: {e}"
                    print(f"[facts] idx_facts_doubt 建索引失败,critique 走全扫降级({e})", file=sys.stderr)
        if self.schema_version() < SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()
        out["to"] = self.schema_version()
        return out

    # ── 带质疑行的取数(critique / deep-check)──────────────────────────────
    _SEV_CASE = ("CASE json_extract(extra,'$.doubt_severity') "
                 "WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END")

    def query_with_doubts(self, limit: int = 5000, categories: Optional[list] = None,
                          code_only: bool = False) -> list[dict]:
        """只取带质疑标记的 active/disputed 事实,按严重度降序。

        此前 critique/deep-check 用 query(limit=5000) 按成色降序截断——生产库前 5000 条全是 A 级
        公告、A 级带质疑数为 0,两个功能在当前库上恒为空且不报错(2026-08-27 审核 F1)。
        有 idx_facts_doubt 时走部分索引(毫秒);没有则 LIKE 全扫降级(~15s,出声)。
        `+status` 禁用 status 索引(规划器坑,见 _search_fts)。"""
        sev = self._SEV_CASE
        if self.has_doubt_index():
            where = "json_extract(extra,'$.doubt_severity') IS NOT NULL AND +status IN ('active','disputed')"
        else:
            self._warn_once("idx_facts_doubt 未建(跑 ./tkb migrate),质疑取数走全表扫描")
            where = """extra LIKE '%"doubt_severity": "%' AND +status IN ('active','disputed')"""
        args: list = []
        if categories:
            where += f" AND +category IN ({','.join('?' * len(categories))})"
            args += list(categories)
        if code_only:
            where += " AND (+canonical_id LIKE 'SH______' OR +canonical_id LIKE 'SZ______' OR +canonical_id LIKE 'BJ______')"
        sql = (f"SELECT * FROM facts WHERE {where} ORDER BY {sev} DESC, support_count DESC LIMIT ?")
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def count_with_doubts(self) -> int:
        if self.has_doubt_index():
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM facts WHERE json_extract(extra,'$.doubt_severity') IS NOT NULL "
                "AND +status IN ('active','disputed')").fetchone()[0])
        return int(self.conn.execute(
            """SELECT COUNT(*) FROM facts WHERE extra LIKE '%"doubt_severity": "%' """
            "AND +status IN ('active','disputed')").fetchone()[0])

    def doc_claim_build(self, batch: int = 50000) -> dict:
        """一次性/对账回填:先 migrate(主键迁移等)与清悬空,再两遍登记(active/disputed 先,其余后),
        INSERT OR IGNORE,幂等。

        按 rowid 分页读、读完关游标再写:WAL 下若在打开的读快照期间别的连接提交了写,本连接再写就报
        BUSY_SNAPSHOT("database is locked"),重试也无用——08-27 与日更 ingest 并行时踩过。"""
        self.migrate()
        dangling = self.doc_claim_clean_dangling()
        n = seen = 0
        for cond in ("status IN ('active','disputed')", "status NOT IN ('active','disputed')"):
            last = -1
            while True:
                page = self.conn.execute(
                    f"SELECT rowid, fact_id, claim, sources FROM facts WHERE {cond} AND rowid > ? "
                    f"ORDER BY rowid LIMIT ?", (last, batch)).fetchall()
                if not page:
                    break
                last = page[-1]["rowid"]
                seen += len(page)
                rows = []
                for r in page:
                    try:
                        srcs = json.loads(r["sources"] or "[]")
                    except ValueError:
                        srcs = []
                    ck = self.claim_key(r["claim"])
                    rows.extend((d, ck, r["fact_id"]) for d in srcs if d)
                n += self._doc_claim_flush(rows)
        total = self.conn.execute("SELECT COUNT(*) FROM doc_claim").fetchone()[0]
        return {"facts_scanned": seen, "inserted": n, "doc_claim_rows": total,
                "dangling_removed": dangling}

    def _doc_claim_flush(self, rows: list) -> int:
        """分批落盘;与日更 ingest 同时跑时写锁可能超过 busy_timeout,锁冲突退避重试而非整跑失败。"""
        import time as _time
        before = self.conn.total_changes
        for attempt in range(12):
            try:
                self.conn.executemany("INSERT OR IGNORE INTO doc_claim(doc_id, ckey, fact_id) VALUES(?,?,?)", rows)
                self.conn.commit()
                return self.conn.total_changes - before
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == 11:
                    raise
                self.conn.rollback()
                _time.sleep(5 * (attempt + 1))
        return 0

    def doc_claim_status(self) -> dict:
        rows = self.conn.execute("SELECT COUNT(*) FROM doc_claim").fetchone()[0]
        dangling = self.conn.execute(
            "SELECT COUNT(*) FROM doc_claim c LEFT JOIN facts f ON f.fact_id=c.fact_id WHERE f.fact_id IS NULL"
        ).fetchone()[0]
        return {"rows": rows, "dangling": dangling}

    def _migrate_ingested_at(self) -> None:
        """2026-08-26:加 ingested_at(入库时刻,ISO 秒级)。只做回溯与 valid_at≤ingested_at 校验,
        永不参与排序/时效加分。ADD COLUMN 常量默认值是元数据操作,2.5GB 库也秒级;多进程并发
        同时迁移时后到者报 duplicate column,吞掉即可。存量行留 ''(=不知道)。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(facts)")}
        if "ingested_at" in cols:
            return
        try:
            self.conn.execute("ALTER TABLE facts ADD COLUMN ingested_at TEXT DEFAULT ''")
            self.conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

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
            -- FTS5 关键词索引的旁表(2026-08-25):fts_map 把 FTS rowid 映射到 fact_id
            -- (故意不用 facts.rowid:TEXT 主键表的 rowid 会被 VACUUM 重编);fts_meta 记
            -- "索引是否已全量建成"——半建索引绝不能冒充全量参与检索。
            CREATE TABLE IF NOT EXISTS fts_map (
                id       INTEGER PRIMARY KEY,
                fact_id  TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS fts_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            -- background 留痕表(2026-08-26 v3):分流判 background 的 finding 此前直接丢弃、无痕
            -- 不可审计;现在原文落这里(不进 FTS/向量、不参与检索),供审计与日后规则演进回填。
            -- 主键 = sha1(doc_id|claim),同一卡片同一论断幂等。
            CREATE TABLE IF NOT EXISTS background_log (
                log_id      TEXT PRIMARY KEY,
                doc_id      TEXT,
                claim       TEXT,
                entities    TEXT,
                source_date TEXT,
                source_kind TEXT,
                reason      TEXT,
                logged_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bglog_doc ON background_log(doc_id);
            """
        )
        self.conn.commit()

    # ── background 留痕(v3)────────────────────────────────────────────────
    def log_background(self, doc_id: str, claim: str, entities=None, source_date: str = "",
                       source_kind: str = "", reason: str = "no_entity_no_number") -> str:
        """记录一条被分流丢弃的 finding 原文(幂等,INSERT OR IGNORE)。返回 log_id。"""
        import hashlib
        from datetime import datetime
        log_id = hashlib.sha1(f"{doc_id}|{claim}".encode("utf-8")).hexdigest()[:16]
        self.conn.execute(
            """INSERT OR IGNORE INTO background_log
               (log_id,doc_id,claim,entities,source_date,source_kind,reason,logged_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (log_id, doc_id or "", claim or "",
             json.dumps([e for e in (entities or []) if isinstance(e, str)], ensure_ascii=False),
             source_date or "", source_kind or "", reason,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.conn.commit()
        return log_id

    def background_log_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM background_log").fetchone()[0])

    def set_extra_entities(self, fact_id: str, entities: list) -> bool:
        """P1 存量补录:把实体列表**并入** extra.entities 并改写 FTS 行(不改 id/主体)。
        返回是否找到该事实。单条一事务;批量请用 set_extra_entities_many(2026-08-26:回填 research lane
        每条一事务 + fsync 只有 ~20 条/s,批量后一事务 500 条)。"""
        with self.conn:
            return self._set_extra_entities_inner(fact_id, entities)

    def set_extra_entities_many(self, items) -> int:
        """批量版:items=[(fact_id, entities)],一个事务内逐条并入。返回找到并处理的条数。"""
        n = 0
        with self.conn:
            for fid, ents in items:
                if self._set_extra_entities_inner(fid, ents):
                    n += 1
        return n

    def _set_extra_entities_inner(self, fact_id: str, entities: list) -> bool:
        """(调用方事务内)并集写 extra.entities + FTS 改写;找不到返回 False。"""
        row = self.conn.execute(
            "SELECT claim, object, subject, extra FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        if row is None:
            return False
        try:
            extra = json.loads(row["extra"] or "{}")
        except (TypeError, ValueError):
            extra = {}
        # 并集而非整体替换(2026-08-26 审查 P2-5):同一事实可能被多张卡/多轮回填补录,后来者不能抹掉先前实体
        ents = list(extra.get("entities") or [])
        for e in entities:
            if isinstance(e, str) and e.strip() and e not in ents:
                ents.append(e)
        if extra.get("entities") == ents:
            return True
        extra["entities"] = ents
        self.conn.execute("UPDATE facts SET extra=? WHERE fact_id=?",
                          (json.dumps(extra, ensure_ascii=False), fact_id))
        self._fts_write(fact_id, row["claim"], row["object"], row["subject"], " ".join(ents))
        return True

    # ── FTS5 关键词索引(P0-B,2026-08-25,docs/RECALL_FIX_PLAN_20260825.md)──────
    # 旧 search 是 LIKE 全表扫:"需求"这类高频 gram 单独命中 6 万行,ORDER BY rowid DESC
    # LIMIT 400 被最新入库的无关事实灌满(实测球硅查询 0/400),且截断早于相关性排序;
    # 单 token LIKE 全扫 110 万行要 14~28s,LIKE 路线不可改良。改为 content_grams 预切
    # 2-gram 空格连接 → unicode61 分词(每个 gram 一个 token)+ bm25 排序:截断发生在相关性
    # 之后,IDF 自动压低高频词。不用 FTS5 自带 trigram:它对 <3 字的子串一律不命中,而
    # "球硅/燃机/HBM"这类 2 字词正是 A 股语料主力。
    def _init_fts(self) -> None:
        """建 FTS5 表(contentless + contentless_delete,需 SQLite>=3.43);不可用 → LIKE 降级并出声。
        空库(新建/测试)直接标 built:此后每条 upsert 同事务写索引,索引从一开始就完整;
        存量库须 `tkb fts build` 全量对账后才启用。"""
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5("
                "grams, content='', contentless_delete=1, tokenize='unicode61')")
            self.conn.commit()
            self._fts_ok = True
        except sqlite3.OperationalError as e:
            self.conn.rollback()
            self._fts_ok = False
            self._warn_once(f"FTS5 不可用({e}),关键词召回走 LIKE 降级")
            return
        if not self._fts_built():
            if self.conn.execute("SELECT 1 FROM facts LIMIT 1").fetchone() is None:
                self._set_fts_built()

    @staticmethod
    def _warn_once(msg: str) -> None:
        if msg not in _WARNED:
            _WARNED.add(msg)
            print(f"[facts] {msg}", file=sys.stderr)

    def _fts_built(self) -> bool:
        row = self.conn.execute("SELECT value FROM fts_meta WHERE key='built'").fetchone()
        return bool(row and row[0] == "1")

    def _set_fts_built(self) -> None:
        self.conn.execute("INSERT OR REPLACE INTO fts_meta(key, value) VALUES('built','1')")
        self.conn.commit()

    @staticmethod
    def _fts_grams(claim, obj, subj, entities: str = "") -> str:
        """索引文本:claim+object+subject(+extra.entities 全部实体名,v3/P1)的 content_grams
        (与检索/冲突消解同一基元)空格连接。实体名进索引是为了让"挂在浩通科技名下、
        entities 里还有晓程科技"的多实体事实也能被"晓程"查到(主体只取首实体,其余此前全丢)。"""
        return " ".join(sorted(content_grams(
            f"{claim or ''} {obj or ''} {subj or ''} {entities or ''}")))

    @staticmethod
    def _entities_text(extra) -> str:
        """从 extra(dict 或 JSON 串)取 entities 列表拼成文本;缺失/畸形返回空串。"""
        try:
            if isinstance(extra, str):
                extra = json.loads(extra or "{}")
            ents = (extra or {}).get("entities") or []
            return " ".join(e for e in ents if isinstance(e, str))
        except (TypeError, ValueError, AttributeError):
            return ""

    def _fts_write(self, fact_id: str, claim, obj, subj, entities: str = "") -> None:
        """(在调用方事务内)写/改写一条索引。同 fact_id 已有映射 → 先删旧 FTS 行再写。"""
        if not self._fts_ok:
            return
        row = self.conn.execute("SELECT id FROM fts_map WHERE fact_id=?", (fact_id,)).fetchone()
        if row:
            rid = row[0]
            self.conn.execute("DELETE FROM facts_fts WHERE rowid=?", (rid,))
        else:
            rid = self.conn.execute("INSERT INTO fts_map(fact_id) VALUES(?)", (fact_id,)).lastrowid
        self.conn.execute("INSERT INTO facts_fts(rowid, grams) VALUES(?,?)",
                          (rid, self._fts_grams(claim, obj, subj, entities)))

    @staticmethod
    def _fts_terms(text: str) -> list:
        """查询项:content_grams(去停用 gram)∪ 用户明确输入的整词(仅当其本身就是索引 token:
        2 字中文或英数词;"股份"作为切出的 gram 被停用,但用户整词查"股份"仍可查)。"""
        words = [t for t in re.split(r"[\s,，、;；。]+", text or "") if 2 <= len(t) <= 40]
        whole = [w.lower() for w in words
                 if (len(w) == 2 and not w.isascii()) or re.fullmatch(r"[A-Za-z0-9]{2,}", w)]
        grams = [g for g in content_grams(text or "") if len(g) >= 2 and g not in _STOP_GRAMS]
        return list(dict.fromkeys(whole + grams))[:40]

    def _search_fts(self, text: str, canonical_id: Optional[str], limit: int) -> list[dict]:
        terms = self._fts_terms(text)
        if not terms:
            return self._search_like(text, canonical_id, False, limit)
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        fetch = limit * 2 if canonical_id else int(limit * 1.3) + 20   # 回表过滤 status 会掉一些
        fids = [r[0] for r in self.conn.execute(
            "SELECT m.fact_id FROM facts_fts JOIN fts_map m ON m.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? ORDER BY bm25(facts_fts) LIMIT ?", (match, fetch))]
        by_id: dict = {}
        for i in range(0, len(fids), 400):
            chunk = fids[i:i + 400]
            # `+status` / `+canonical_id`:一元加号禁止规划器对该列用索引。否则它会选
            # idx_facts_status 扫全部 110 万 active 行再过滤 IN 列表(实测 6~9s),而非
            # 按 fact_id 主键点查 400 条(0.01s)。
            sql = (f"SELECT * FROM facts WHERE fact_id IN ({','.join('?' * len(chunk))}) "
                   "AND +status IN ('active','disputed')")
            args: list = list(chunk)
            if canonical_id:
                sql += " AND +canonical_id=?"
                args.append(canonical_id)
            for r in self.conn.execute(sql, args):
                by_id[r["fact_id"]] = dict(r)
        out = []
        for fid in fids:                       # 保 bm25 序;索引漂移(fact 已删/改状态)只丢名额不出错
            f = by_id.get(fid)
            if f is not None:
                out.append(f)
                if len(out) >= limit:
                    break
        return out

    def fts_build(self, batch: int = 20000) -> dict:
        """FTS5 索引对账:补缺(active/disputed 里未索引的)+ 清孤儿(索引里已不活跃、或被绕过
        FactsStore 的 raw-SQL 治理脚本删/改 fact_id 的),完成后标记 built。可反复跑;
        首建 1.1M 行实测约 1 分钟。与 semantic.build 同一自愈模式,挂日常 --tail-only。"""
        if not self._fts_ok:
            raise RuntimeError("FTS5 不可用(SQLite 过旧或无 fts5 扩展)")
        active = {r[0] for r in self.conn.execute(
            "SELECT fact_id FROM facts WHERE status IN ('active','disputed')")}
        mapped = {r[0]: r[1] for r in self.conn.execute("SELECT fact_id, id FROM fts_map")}
        missing = [f for f in active if f not in mapped]
        orphans = [(mapped[f],) for f in mapped if f not in active]
        added = 0
        for i in range(0, len(missing), batch):
            chunk = missing[i:i + batch]
            rows = []
            for j in range(0, len(chunk), 500):
                sub = chunk[j:j + 500]
                rows += self.conn.execute(
                    "SELECT fact_id, claim, object, subject, extra FROM facts "
                    f"WHERE fact_id IN ({','.join('?' * len(sub))})", sub).fetchall()
            with self.conn:                    # 每批一个事务,写锁有界
                for r in rows:
                    self._fts_write(r["fact_id"], r["claim"], r["object"], r["subject"],
                                    self._entities_text(r["extra"]))
            added += len(rows)
        if orphans:
            with self.conn:
                self.conn.executemany("DELETE FROM facts_fts WHERE rowid=?", orphans)
                self.conn.executemany("DELETE FROM fts_map WHERE id=?", orphans)
        if added or orphans:
            with self.conn:
                self.conn.execute("INSERT INTO facts_fts(facts_fts) VALUES('optimize')")
        self._set_fts_built()
        return {"added": added, "removed": len(orphans), "indexed": len(active)}

    def fts_status(self) -> dict:
        """cli status 用:可用/已启用/已索引条数/应索引条数。"""
        indexed = (int(self.conn.execute("SELECT COUNT(*) FROM fts_map").fetchone()[0])
                   if self._fts_ok else 0)
        return {"ok": self._fts_ok, "built": self._fts_ok and self._fts_built(),
                "indexed": indexed, "active": self.count_active()}

    # ── 写入(含去重合并)─────────────────────────────────────────────────
    def upsert(self, fact: Fact, _depth: int = 0) -> str:
        """写入事实;dedup_key 命中则合并(累加来源/升级成色/保留时间线)。

        返回 fact_id。重复执行幂等(§18 deterministic id)。
        _depth:内部递归护栏(极端"插入-删除"循环下防无限递归)。
        """
        if _depth > 3:
            raise sqlite3.OperationalError("facts.upsert 插入/删除竞态循环超过 3 层")
        # 日期闸口(2026-08-26):`2026-84-17` 这类抽错的日期一律置空;否则合并路径 min() 会让
        # 垃圾字符串赢过真日期。
        fact.valid_at = clean_date(fact.valid_at)
        self.last_upsert_dup = False
        # (doc, claim) 判重(2026-08-27):同一文档同一句已有行 → 视为同一事实,不再按新 fact_id 插入。
        # 例外:两行分别挂**不同**证券代码(同一句刻意拆到两只股票,dedup 也整组保留的那类)。
        # 命中 superseded 行也算存在——不复活(与 backfill_background 口径一致)。
        if _depth == 0:
            same = self.find_doc_claim_dup(fact.sources, fact.claim, fact.canonical_id)
            if same is not None:
                self.last_upsert_dup = True
                ents = (fact.extra or {}).get("entities") if isinstance(fact.extra, dict) else None
                if ents and same["status"] in ("active", "disputed"):
                    try:
                        self.set_extra_entities(same["fact_id"], ents)
                    except Exception as e:          # §2.2:降级可以,无声不行
                        self._warn_once(f"判重命中后补 extra.entities 失败({type(e).__name__}: {e})")
                return same["fact_id"]
        existing = self.conn.execute(
            "SELECT * FROM facts WHERE dedup_key=?", (fact.dedup_key,)
        ).fetchone()

        if existing is None:
            row = fact.to_row()
            row["unverifiable"] = int(fact.unverifiable)
            row["ingested_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            try:
                self.conn.execute(
                    """INSERT INTO facts
                       (fact_id,dedup_key,subject,predicate,object,canonical_id,claim,status,
                        evidence_level,unverifiable,source_kind,support_count,sources,valid_at,
                        invalid_at,supersedes,relation_id,category,extra,ingested_at)
                       VALUES
                       (:fact_id,:dedup_key,:subject,:predicate,:object,:canonical_id,:claim,:status,
                        :evidence_level,:unverifiable,:source_kind,:support_count,:sources,:valid_at,
                        :invalid_at,:supersedes,:relation_id,:category,:extra,:ingested_at)""",
                    row,
                )
                # 同事务写 FTS 索引(P0-B);索引写失败只出声,绝不让事实本身落库失败
                # (漂移由 fts_build 日常对账兜底)。
                try:
                    self._fts_write(fact.fact_id, row.get("claim"), row.get("object"),
                                    row.get("subject"), self._entities_text(fact.extra))
                except sqlite3.OperationalError as e:
                    self._warn_once(f"FTS 索引写入失败,待 fts build 对账({e})")
                self.doc_claim_register(fact.fact_id, fact.sources, fact.claim)
                self.conn.commit()
                return fact.fact_id
            except sqlite3.IntegrityError:
                # 并发竞态修复(TOCTOU):另一连接在本次 SELECT 与 INSERT 之间抢先插入了同
                # dedup_key/fact_id。回滚未提交写入,重新读出已存在行,落到下方合并路径——
                # 与 EntityRegistry.register 的 INSERT OR IGNORE 同思路,保证多线程 web 入库不抛 500。
                self.conn.rollback()
                existing = self.conn.execute(
                    "SELECT * FROM facts WHERE dedup_key=?", (fact.dedup_key,)
                ).fetchone()
                if existing is None:
                    raise
            except Exception:
                # 任何其他异常(含 idx_facts_doubt 建成后写坏 extra 抛的 malformed JSON)都不能留下
                # 悬挂事务:否则"事实已插、doc_claim 未登记"的半成品会被下一次 upsert 的 commit
                # 一并提交(按卡吞异常的 ingest_kb_cards 必踩)。回滚后原样抛出。
                self.conn.rollback()
                raise

        # 合并:累加来源、升级成色、support_count、保留最早 valid_at。
        # 乐观并发:UPDATE 带 `sources=旧值` 条件,被别的连接抢先改了(rowcount=0)则重读重试——
        # 否则两请求同时写同一 fact 时,各自基于陈旧 sources 的 read-modify-write 会 lost-update,
        # 丢源、support_count 少计(busy_timeout 只串行化写事务本身,挡不住跨连接的读改写竞态)。
        for _ in range(8):
            old_sources_json = existing["sources"]
            merged_sources = sorted(set(json.loads(old_sources_json or "[]") + fact.sources))
            # `or 1`:两侧 sources 均空时不应把已存在事实的 support_count 覆盖为 0(口径同 INSERT)
            support = len(merged_sources) or 1
            best_level = _max_level(existing["evidence_level"], fact.evidence_level)
            # unverifiable 仅当"两条都未经数据验证"时才保持 True。
            # 注:unverifiable=False 只由 grade 的数据验证(confirmed/refuted)产生,
            # 故 AND 语义 = "任一来源经数据验证则整体已验证",是真实的多源印证,非洗白(M4)。
            unver = int(bool(existing["unverifiable"]) and fact.unverifiable)
            valid_at = min(filter(None, [existing["valid_at"], fact.valid_at]), default=fact.valid_at)
            new_sources_json = json.dumps(merged_sources, ensure_ascii=False)
            # 同 key 再次出现(如同一论断被再次确认):若旧行已被 superseded/invalidated,
            # 在合并时复活为 active(C1:避免 supersede 自碰撞导致事实丢失)。
            revive = existing["status"] in ("superseded", "invalidated", "expired")
            if revive:
                cur = self.conn.execute(
                    """UPDATE facts SET sources=?, support_count=?, evidence_level=?, unverifiable=?,
                                         valid_at=?, status='active', invalid_at=NULL
                       WHERE dedup_key=? AND sources=?""",
                    (new_sources_json, support, best_level, unver, valid_at,
                     fact.dedup_key, old_sources_json),
                )
            else:
                cur = self.conn.execute(
                    """UPDATE facts SET sources=?, support_count=?, evidence_level=?, unverifiable=?,
                                         valid_at=? WHERE dedup_key=? AND sources=?""",
                    (new_sources_json, support, best_level, unver, valid_at,
                     fact.dedup_key, old_sources_json),
                )
            if cur.rowcount:
                # 新 doc 并入 sources 后登记:旧 claim 与新 claim 各登记一次——dedup_key 的 object
                # 只取 claim[:80],80 字后不同的 claim 变体也走到这里;只登记旧 claim 会让新变体
                # 换 cid 再入库时绕过判重(2026-08-27 审核 F5)。
                self.doc_claim_register(existing["fact_id"], fact.sources, existing["claim"])
                if _normalize(fact.claim or "") != _normalize(existing["claim"] or ""):
                    self.doc_claim_register(existing["fact_id"], fact.sources, fact.claim)
            self.conn.commit()
            if cur.rowcount:
                return existing["fact_id"]
            # rowcount==0:sources 被别的连接抢先改了,重读最新行重试
            existing = self.conn.execute(
                "SELECT * FROM facts WHERE dedup_key=?", (fact.dedup_key,)
            ).fetchone()
            if existing is None:                       # 期间被删,回顶层重走 INSERT 路径
                return self.upsert(fact, _depth + 1)
        raise sqlite3.OperationalError("facts.upsert 合并乐观重试 8 次仍冲突(并发异常)")

    # ── 状态变更 ──────────────────────────────────────────────────────────
    def supersede(self, old_fact_id: str, new_fact: Fact, at: str) -> str:
        """新事实替代旧事实:旧标 superseded + invalid_at,新事实记 supersedes。

        C1 修复:若新旧是同一事实(同 dedup_key/fact_id),不能"标旧 superseded 再 upsert"
        ——那样新事实会落到同一行并被标记消失。此时改为原地合并复活(active)。
        """
        if new_fact.fact_id == old_fact_id:
            # 同一论断再次确认:不自我替代,走 upsert 的复活+合并路径(见 upsert)
            return self.upsert(new_fact)
        # 先落新事实,再在**单事务**里"标旧 superseded + 写血缘":中途崩溃最多留下
        # 新旧短暂并存(良性,重跑收敛),不会出现"旧已标替代、新未落库"的孤儿状态
        # (v0.4 三段式各自 commit 的缺陷)。
        new_fact.supersedes = sorted(set(new_fact.supersedes + [old_fact_id]))
        dup_before = self.last_upsert_dup
        nid = self.upsert(new_fact)
        if nid != new_fact.fact_id:
            # 新事实被 (doc, claim) 判重拦到了**另一行**(同文同句已有行):它不是"新事实",
            # 不能拿它去替代旧事实——否则血缘写到别的行、旧行被错标 superseded(审核 A P2-10)。
            self._warn_once(f"supersede 跳过:新事实与已有行 {nid} 判重同一,未标 {old_fact_id} 为 superseded")
            self.last_upsert_dup = dup_before
            return nid
        self.last_upsert_dup = dup_before      # supersede 内部的 upsert 不改变"上一次 upsert 是否判重"
        # B4:upsert 合并路径不写 supersedes 列,这里显式落库替代血缘
        existing = self.conn.execute(
            "SELECT supersedes FROM facts WHERE fact_id=?", (nid,)
        ).fetchone()
        prev = json.loads(existing["supersedes"]) if existing and existing["supersedes"] else []
        merged = sorted(set(prev) | set(new_fact.supersedes))
        with self.conn:
            self.conn.execute(
                "UPDATE facts SET status='superseded', invalid_at=? WHERE fact_id=?",
                (at, old_fact_id),
            )
            self.conn.execute("UPDATE facts SET supersedes=? WHERE fact_id=?",
                              (json.dumps(merged, ensure_ascii=False), nid))
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
              include_invalidated: bool = False, limit: int = 100,
              levels: Optional[list] = None, order: str = "level") -> list[dict]:
        """检索事实。默认只返 active/disputed;include_invalidated=True 返历史(§10.3 审计)。

        levels: 只取指定成色档(如 ["C","D"])。供 ask 候选池定向补录低成色观点——
                默认成色降序 + LIMIT 会把重覆盖个股(高成色 >limit)的 C/D 全部截掉,
                情绪面段静默失效(P0-1 反饿死)。
        order : "level"=成色降序(默认,原行为) / "recent"=valid_at 降序(取最新边际,
                不分成色,治"最新研报/社媒进不了池"的时效饿死)。
        """
        sql = "SELECT * FROM facts WHERE 1=1"
        args: list = []
        if canonical_id:
            sql += " AND canonical_id=?"
            args.append(canonical_id)
        if predicate:
            sql += " AND predicate=?"
            args.append(predicate)
        if levels:
            sql += f" AND evidence_level IN ({','.join('?' * len(levels))})"
            args += list(levels)
        if not include_invalidated:
            sql += " AND status IN ('active','disputed')"
        if order == "recent":
            # 时效序:valid_at 缺失排最后;同日多源在前
            sql += " ORDER BY COALESCE(valid_at,'') DESC, support_count DESC LIMIT ?"
        else:
            # 成色排序修正：evidence_level 是 TEXT，DESC 字符串序会把 'D' 排到 'A' 前
            # （'D'>'C'>'B+'>'B'>'A'），与"高成色优先"意图相反。
            # CASE 从 models.LEVEL_RANK 程序化生成(单一定义点,加档位自动跟随)。
            case = " ".join(f"WHEN '{lv}' THEN {rk}" for lv, rk in _LEVEL_RANK.items())
            sql += (f" ORDER BY CASE evidence_level {case} ELSE 0 END DESC,"
                    " support_count DESC LIMIT ?")
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def search(self, text: str, canonical_id: Optional[str] = None,
               include_invalidated: bool = False, limit: int = 400) -> list[dict]:
        """文本召回:FTS5 bigram + BM25(2026-08-25),截断发生在相关性排序**之后**。

        供 ask 在候选池上做 gram+成色+时效加权排序。降级链:FTS 不可用 / 索引未建
        (`tkb fts build`)/ 查询异常 / include_invalidated=True(只 LIKE 支持)→ 旧 LIKE 预筛
        (出声)。返回顺序:FTS 路径按 bm25 相关性;LIKE 路径按 rowid 降序(新入库优先)。
        """
        self.last_search_mode = "fts"          # ask 据此在 warnings 里出声(§2.2),不只靠 stderr
        if self._fts_ok and not include_invalidated:
            if self._fts_built():
                try:
                    return self._search_fts(text, canonical_id, limit)
                except sqlite3.OperationalError as e:
                    self._warn_once(f"FTS 查询异常,降级 LIKE({e})")
            else:
                self._warn_once("FTS 索引未建(跑 ./tkb fts build),关键词召回走 LIKE 降级")
        self.last_search_mode = "like"
        return self._search_like(text, canonical_id, include_invalidated, limit)

    def _search_like(self, text: str, canonical_id: Optional[str] = None,
                     include_invalidated: bool = False, limit: int = 400) -> list[dict]:
        """旧版 SQL LIKE 预筛(降级路径保留)。中文无分词,按空白/标点切词后任一 token 命中
        claim/object/subject 即入候选;ORDER BY rowid DESC 截断——高频 token 会灌满名额,
        这正是 FTS 要治的病,仅作 FTS 不可用时的兜底。"""
        import re as _re
        from .models import content_grams as _cg
        # token = 分隔符切的实词(保精确短语如 688627) ∪ content_grams(治无空格中文，
        # 如"多空因子选股效果"整串 LIKE 不中，但 gram 多空/因子/选股/效果 能命中)
        # 上限 40 字:防无分隔超长串(如恶意查询)被切成单个巨 token,生成 LIKE '%<上万字>%'
        # 触发 SQLite "LIKE or GLOB pattern too complex"。真实标的名/关键词远不及 40 字。
        words = [t for t in _re.split(r"[\s,，、;；。]+", text or "") if 2 <= len(t) <= 40]
        # 高频停用 gram(P0-1):"股份/公司"这类 2-gram 在半数以上事实里出现,LIKE 命中几十万行,
        # 无序 LIMIT 截断后返回的全是无关旧公告(实测查"银轮股份"返回 400 条无一相关)。
        # 只过滤切出来的 gram,完整词 token(如用户真查"股份回购")不受影响。
        grams = [g for g in _cg(text or "") if len(g) >= 2 and g not in _STOP_GRAMS]
        toks = list(dict.fromkeys(words + grams))[:24]
        if not toks:
            # 单字/空串查询切不出任何 token:此前无过滤地 ORDER BY rowid DESC LIMIT 400 灌进
            # 400 条无关新行(审核 A P2-6);无 token 就是无召回。
            return []
        sql = "SELECT * FROM facts WHERE 1=1"
        args: list = []
        if not include_invalidated:
            sql += " AND status IN ('active','disputed')"
        if canonical_id:
            sql += " AND canonical_id=?"
            args.append(canonical_id)
        if toks:
            ors = []
            for t in toks:
                # 转义 LIKE 元字符 %_\，避免 token 里的 %/_ (如"净利率50%")被当通配符过度召回
                esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                ors.append("(claim LIKE ? ESCAPE '\\' OR object LIKE ? ESCAPE '\\' "
                           "OR subject LIKE ? ESCAPE '\\')")
                like = f"%{esc}%"
                args += [like, like, like]
            sql += " AND (" + " OR ".join(ors) + ")"
        # rowid 降序(P0-1):原先无 ORDER BY,LIMIT 截断取到的是最早插入的行(多为最老公告)。
        # 改为新入库优先——截断有确定性且偏向新内容;真正的相关性排序交给 ask._rank_facts。
        # 计划器可倒序走表边扫边截,凑满 LIMIT 即停,不比无序全扫慢。
        sql += " ORDER BY rowid DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def get(self, fact_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        return dict(row) if row else None

    def patch_extra(self, fact_id: str, kv: dict) -> bool:
        """给已存在事实的 extra 补**缺失**的键，返回是否真的写了。

        只增不改：已有的键一律不动。upsert 的合并分支只累加来源/升级成色、
        不碰 extra，所以后到的旁证字段(如公告摘要)需要这条路补。
        既然不覆盖已有值,重复执行天然幂等,也不会让后到的源改写原始口径。

        整个读-改-写包在 BEGIN IMMEDIATE 里:busy_timeout 只串行化写事务本身,
        挡不住两个连接各自基于旧 extra 的 read-modify-write 互相覆盖(丢补丁)。
        """
        own_txn = not self.conn.in_transaction      # 嵌套在外层事务里时不接管提交/回滚
        if own_txn:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT extra FROM facts WHERE fact_id=?",
                                    (fact_id,)).fetchone()
            cur = json.loads(row["extra"] or "{}") if row is not None else {}
            add = {k: v for k, v in kv.items() if k not in cur and v not in (None, "")}
            if row is None or not add:
                if own_txn:
                    self.conn.rollback()
                return False
            cur.update(add)
            self.conn.execute("UPDATE facts SET extra=? WHERE fact_id=?",
                              (json.dumps(cur, ensure_ascii=False), fact_id))
            if own_txn:
                self.conn.commit()
            return True
        except Exception:
            if own_txn:
                self.conn.rollback()
            raise

    def count_active(self) -> int:
        """active/disputed 事实数(语义索引覆盖率等上层统计用,替代裸摸 conn)。"""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE status IN ('active','disputed')"
        ).fetchone()[0])

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
        cats = self.conn.execute(
            "SELECT category, COUNT(*) c FROM facts WHERE status IN ('active','disputed') GROUP BY category"
        ).fetchall()
        by_category = {(r["category"] or "?"): r["c"] for r in cats}
        return {"total": total, "by_status": by_status, "by_level": by_level,
                "by_category": by_category,                 # view/hard_fact/quant_fact(历史 #27)
                "background_log": self.background_log_count(),
                "doc_claim": self.doc_claim_status(),
                "fts": self.fts_status(),
                "schema_version": self.schema_version(),
                "doubt_index": self.has_doubt_index()}

    def close(self) -> None:
        self.conn.close()


def _max_level(a: EvidenceLevel, b: EvidenceLevel) -> EvidenceLevel:
    """取更高成色。"""
    return a if _LEVEL_RANK.get(a, 0) >= _LEVEL_RANK.get(b, 0) else b

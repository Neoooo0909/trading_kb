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

from .models import Fact, EvidenceLevel, LEVEL_RANK

_LEVEL_RANK = LEVEL_RANK   # 唯一定义点在 models.LEVEL_RANK;此别名兼容既有导入

# search 的停用 gram:出现在极大比例事实里的 2-gram(公司名后缀/公告套词/泛化行业词),
# 作为 LIKE 条件没有区分度、只会把 LIMIT 名额吃光。仅过滤自动切出的 gram,不影响完整词。
_STOP_GRAMS = {"股份", "公司", "有限", "集团", "科技", "关于", "公告", "市场",
               "行业", "中国", "股东", "计划", "发展", "控股"}


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
    def upsert(self, fact: Fact, _depth: int = 0) -> str:
        """写入事实;dedup_key 命中则合并(累加来源/升级成色/保留时间线)。

        返回 fact_id。重复执行幂等(§18 deterministic id)。
        _depth:内部递归护栏(极端"插入-删除"循环下防无限递归)。
        """
        if _depth > 3:
            raise sqlite3.OperationalError("facts.upsert 插入/删除竞态循环超过 3 层")
        existing = self.conn.execute(
            "SELECT * FROM facts WHERE dedup_key=?", (fact.dedup_key,)
        ).fetchone()

        if existing is None:
            row = fact.to_row()
            row["unverifiable"] = int(fact.unverifiable)
            try:
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
        nid = self.upsert(new_fact)
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
        """文本召回(SQL LIKE 预筛，不受全表扫描上限)。

        供 ask 在候选池上做 gram+成色+时效加权排序。中文无分词，按空白/标点切词后
        任一 token 命中 claim/object/subject 即入候选。
        根治旧 _keyword_facts 只扫前 2000 条导致大库召回坍缩的问题
        (如精智达 167 条社媒事实因成色低排在 17 万条之后，进不了前 2000)。
        """
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
        return {"total": total, "by_status": by_status, "by_level": by_level}

    def close(self) -> None:
        self.conn.close()


def _max_level(a: EvidenceLevel, b: EvidenceLevel) -> EvidenceLevel:
    """取更高成色。"""
    return a if _LEVEL_RANK.get(a, 0) >= _LEVEL_RANK.get(b, 0) else b

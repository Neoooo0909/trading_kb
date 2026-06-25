"""本地语义检索层（P0.5：contextual embedding，bge 优先、model2vec 兜底）。

主进程零依赖（仅用 numpy 算余弦）；embedding 跑在隔离的 .venv-embed(python3.11)，
经 subprocess 调用 → 不污染零依赖核心、3.14 主进程照常跑。两套后端按可用性自动择优：

  • bge        bge-small-zh-v1.5（ONNX，无 torch，contextual transformer，512 维）——粒度细，首选
  • model2vec  potion-zh（静态向量，纯 numpy，256 维）——bge 不可用时降级兜底

每套后端各自一份 vectors_*.db（维度/语义不同，互不混用）。向量预存（增量建），
检索时编码 query + 内存余弦 top-k。venv/模型/numpy 任一缺失或后端全不可用 → shared() 返回
None，ask 自动回退 LIKE+加权（P0-a）。bge 检索遵循其约定：仅对 query 端加检索前缀，
passage 端不加（build 时按原文编码）。
"""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
from pathlib import Path

try:
    import numpy as np
except Exception:                       # numpy 不可用 → 语义层整体降级
    np = None

_PKG = Path(__file__).resolve().parent
_ROOT = _PKG.parent.parent
_VENV_PY = _ROOT / ".venv-embed" / "bin" / "python"

# bge 检索前缀（官方约定：仅加在 query 端，提升短查询召回；passage 端不加）
_BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class _Backend:
    """一套 embedding 后端的描述：worker 脚本 + 模型目录 + 独立向量库 + query 前缀。"""

    def __init__(self, name, worker, model_dir, model_file, vec_db_name,
                 query_prefix="", dim=256):
        self.name = name
        self.worker = worker            # _embed_worker*.py
        self.model_dir = model_dir
        self.model_file = model_file    # 模型目录内必存在的标志文件（判可用）
        self.vec_db_name = vec_db_name
        self.query_prefix = query_prefix
        self.dim = dim

    def available(self) -> bool:
        """venv python + worker 脚本 + 模型标志文件齐备才算可用。"""
        return (_VENV_PY.exists() and self.worker.exists()
                and (self.model_dir / self.model_file).exists())


# 优先级：bge 在前，model2vec 兜底
_BACKENDS = [
    _Backend("bge", _PKG / "_embed_worker_bge.py",
             _ROOT / ".venv-embed" / "bge-small-zh", "model.onnx",
             "vectors_bge.db", query_prefix=_BGE_QUERY_PREFIX, dim=512),
    _Backend("model2vec", _PKG / "_embed_worker.py",
             _ROOT / ".venv-embed" / "potion-zh", "config.json",
             "vectors.db", query_prefix="", dim=256),
]

_SHARED: dict = {}


def _pick_backend(prefer: str | None = None) -> _Backend | None:
    """返回首个可用后端；prefer 指定名字时优先选它（仍要求可用，不可用则按优先级降级）。"""
    cands = _BACKENDS
    if prefer:
        cands = sorted(_BACKENDS, key=lambda b: 0 if b.name == prefer else 1)
    for b in cands:
        if b.available():
            return b
    return None


def _vec_count(facts_db_path, backend: _Backend) -> int:
    """该后端在此 facts 库下已建向量数（供 ask 端自动择"有数据"的后端）。只读、出错记 0。"""
    db = Path(facts_db_path).parent / backend.vec_db_name
    if not db.exists():
        return 0
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return int(c.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return 0


def _auto_backend(facts_db_path) -> _Backend | None:
    """ask 端自动择优：可用后端里挑"已建向量最多"的（并列按优先级 bge>model2vec）。

    根治"build 把向量建进了 model2vec 库、ask 却空读 bge 库"的静默失效：ask 永远用
    实际有数据的那套。全空时 stable sort 保持 _BACKENDS 顺序 → 取优先级首个（bge）。
    """
    avail = [b for b in _BACKENDS if b.available()]
    if not avail:
        return None
    return sorted(avail, key=lambda b: -_vec_count(facts_db_path, b))[0]


class SemanticIndex:
    """向量语义索引：vectors_*.db(fact_id→向量) + 隔离 venv 编码 + 内存余弦检索。"""

    def __init__(self, facts_db_path, backend: _Backend):
        self.facts_db = Path(facts_db_path)
        self.backend = backend
        self.vec_db = self.facts_db.parent / backend.vec_db_name
        self._conn = sqlite3.connect(str(self.vec_db))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors (fact_id TEXT PRIMARY KEY, vec BLOB)")
        self._mat = None        # 归一化向量矩阵 N×D(内存缓存)
        self._ids = None        # 与 _mat 行对齐的 fact_id 列表
        self._lq = None         # 最近一次 query 缓存(省重复 subprocess 编码)
        self._lqv = None

    @classmethod
    def shared(cls, facts_db_path, prefer: str | None = None):
        """按 (db 路径, 后端名) 取单例。numpy/venv/模型全不可用 → None(触发 ask 降级)。

        prefer 指定名字 → 强制该后端（build/status 用，默认 bge）；
        prefer=None → 自动择"已建向量最多"的可用后端（ask 用，避免空读错库）。
        """
        if np is None:
            return None
        backend = _pick_backend(prefer) if prefer else _auto_backend(facts_db_path)
        if backend is None:
            return None
        key = (str(facts_db_path), backend.name)
        if key not in _SHARED:
            try:
                _SHARED[key] = cls(facts_db_path, backend)
            except Exception:
                return None
        return _SHARED[key]

    # ── 编码(隔离 venv subprocess)──────────────────────────────────────
    def _encode(self, texts: list[str]):
        """调隔离 venv 的 worker 编码 → np.ndarray (len×D)，已 L2 归一化。"""
        inp = "\n".join(json.dumps(t) for t in texts)
        r = subprocess.run(
            [str(_VENV_PY), str(self.backend.worker), str(self.backend.model_dir)],
            input=inp.encode("utf-8"), capture_output=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "ignore")[-300:])
        return np.load(io.BytesIO(r.stdout))

    def _encode_query(self, query: str):
        """编码并归一化单条 query（bge 加检索前缀），带最近一次缓存。"""
        if self._lq == query and self._lqv is not None:
            return self._lqv
        text = self.backend.query_prefix + query
        v = self._encode([text])[0].astype("float32")
        v = v / (float(np.linalg.norm(v)) or 1.0)
        self._lq, self._lqv = query, v
        return v

    # ── 建索引(增量)────────────────────────────────────────────────────
    def build(self, facts_store, batch: int = 8000) -> int:
        """对 active/disputed 事实增量建向量(已建的跳过)，返回新增条数。

        **自愈**:建完后清孤儿向量(fact_id 已不在活跃事实里——主体归属/碎片归一改挂会让旧
        fact_id 失效残留),保证向量库与活跃事实 1:1,日更可反复跑不积垃圾、无需手工清理。
        """
        active = {r[0] for r in facts_store.conn.execute(
            "SELECT fact_id FROM facts WHERE status IN ('active','disputed')")}
        done = {r[0] for r in self._conn.execute("SELECT fact_id FROM vectors")}
        rows = facts_store.conn.execute(
            "SELECT fact_id, claim, object FROM facts WHERE status IN ('active','disputed')"
        ).fetchall()
        todo = [(r["fact_id"], f"{r['claim'] or ''} {r['object'] or ''}".strip())
                for r in rows if r["fact_id"] not in done]
        n = 0
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            vecs = self._encode([t for _, t in chunk]).astype("float32")
            self._conn.executemany(
                "INSERT OR REPLACE INTO vectors(fact_id, vec) VALUES (?,?)",
                [(fid, vecs[j].tobytes()) for j, (fid, _) in enumerate(chunk)])
            self._conn.commit()
            n += len(chunk)
        orphans = [fid for fid in (done | {f for f, _ in todo}) if fid not in active]
        if orphans:                          # 清孤儿:改挂后旧 fact_id 残留 → 向量库=活跃事实
            self._conn.executemany("DELETE FROM vectors WHERE fact_id=?", [(x,) for x in orphans])
            self._conn.commit()
        self._mat = self._ids = None        # 失效内存缓存
        return n

    # ── 检索 ────────────────────────────────────────────────────────────
    def _load_matrix(self) -> None:
        """加载全部向量到内存并归一化(余弦=点积)。"""
        if self._mat is not None:
            return
        ids, vecs = [], []
        for fid, blob in self._conn.execute("SELECT fact_id, vec FROM vectors"):
            ids.append(fid)
            vecs.append(np.frombuffer(blob, dtype="float32"))
        if not vecs:
            self._ids, self._mat = [], np.zeros((0, 1), dtype="float32")
            return
        m = np.vstack(vecs)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._mat = m / norms
        self._ids = ids

    def search(self, query: str, top_k: int = 120) -> list[str]:
        """语义召回 top-k fact_ids(召回字面不匹配但语义相关的事实)。"""
        try:
            self._load_matrix()
            if not self._ids:
                return []
            sims = self._mat @ self._encode_query(query)
            order = np.argsort(-sims)[:top_k]
            return [self._ids[i] for i in order]
        except Exception:
            return []

    def score(self, query: str, fact_ids: list) -> dict:
        """对给定 fact_ids 返回语义相似度 {fid: 0~1}；缺失/异常返回空。"""
        try:
            self._load_matrix()
            if not self._ids:
                return {}
            pos = {fid: i for i, fid in enumerate(self._ids)}
            qn = self._encode_query(query)
            out = {}
            for fid in fact_ids:
                i = pos.get(fid)
                if i is not None:
                    out[fid] = max(0.0, float(self._mat[i] @ qn))
            return out
        except Exception:
            return {}

    def close(self) -> None:
        """关闭 sqlite 连接（长驻调用方如 web.py 收尾用；CLI 一次性进程靠退出回收）。"""
        try:
            self._conn.close()
        except Exception:
            pass

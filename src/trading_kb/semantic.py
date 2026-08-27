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
import os
import sqlite3
import subprocess
import sys
import threading
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
_SHARED_LOCK = threading.Lock()     # 多线程 web 下防单例被并发重复创建


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
        # check_same_thread=False + RLock:_SHARED 单例被 web ThreadingHTTPServer
        # 跨请求线程复用,默认线程绑定会抛 ProgrammingError 且被吞 → 语义召回
        # 静默归零(v0.4 暗坑)。所有库操作统一持锁串行化。
        self._conn = sqlite3.connect(str(self.vec_db), check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")   # 与 kb_sync 的 build 并发读写
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors (fact_id TEXT PRIMARY KEY, vec BLOB)")
        self._lock = threading.RLock()
        self._mat = None        # 归一化向量矩阵 N×D(内存缓存)
        self._ids = None        # 与 _mat 行对齐的 fact_id 列表
        self._mat_ver = None    # 加载矩阵时的库版本指纹(mtime/size),外部重建后自动失效
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
        with _SHARED_LOCK:
            if key not in _SHARED:
                try:
                    _SHARED[key] = cls(facts_db_path, backend)
                except Exception:
                    return None
            return _SHARED[key]

    # ── 编码(隔离 venv subprocess)──────────────────────────────────────
    def _encode(self, texts: list[str], timeout: int = 1800):
        """调隔离 venv 的 worker 编码 → np.ndarray (len×D)，已 L2 归一化。

        timeout:批量建库默认 1800s;单条 query 编码调用方须传短超时——
        v0.4 曾共用 1800s,worker 卡死时一次问答挂 30 分钟。
        """
        inp = "\n".join(json.dumps(t) for t in texts)
        r = subprocess.run(
            [str(_VENV_PY), str(self.backend.worker), str(self.backend.model_dir)],
            input=inp.encode("utf-8"), capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "ignore")[-300:])
        return np.load(io.BytesIO(r.stdout))

    def _encode_query(self, query: str):
        """编码并归一化单条 query（bge 加检索前缀），带最近一次缓存。60s 超时。"""
        if self._lq == query and self._lqv is not None:
            return self._lqv
        text = self.backend.query_prefix + query
        v = self._encode([text], timeout=60)[0].astype("float32")
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
            with self._lock:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO vectors(fact_id, vec) VALUES (?,?)",
                    [(fid, vecs[j].tobytes()) for j, (fid, _) in enumerate(chunk)])
                self._conn.commit()
            n += len(chunk)
        orphans = [fid for fid in done if fid not in active]
        with self._lock:
            if orphans:                      # 清孤儿:改挂后旧 fact_id 残留 → 向量库=活跃事实
                self._conn.executemany("DELETE FROM vectors WHERE fact_id=?",
                                       [(x,) for x in orphans])
                self._conn.commit()
            self._mat = self._ids = self._mat_ver = None    # 失效内存缓存
            if n or orphans:
                # 库变了 → 顺手把矩阵磁盘缓存(P1-D)也刷新,别留给第一个 ask 用 210s 重建。
                # 先 TRUNCATE checkpoint:缓存指纹取主文件 mtime/size,若留到连接关闭时再
                # checkpoint,主文件随后又变 → 刚写的缓存立刻失效。
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._load_matrix()
                except Exception as e:
                    print(f"[semantic] build 后刷新矩阵缓存失败(下次 ask 会重建):"
                          f"{type(e).__name__}: {e}", file=sys.stderr)
        return n

    # ── 检索 ────────────────────────────────────────────────────────────
    def _db_version(self) -> tuple:
        """向量库版本指纹(主文件 + -wal 的 mtime/size):外部进程重建后据此失效缓存。"""
        v = []
        for p in (self.vec_db, Path(str(self.vec_db) + "-wal")):
            try:
                st = p.stat()
                v.append((st.st_mtime_ns, st.st_size))
            except OSError:
                v.append(None)
        return tuple(v)

    # ── 矩阵磁盘缓存(P1-D,2026-08-25)────────────────────────────────────────
    # 每个进程从 sqlite BLOB 逐行读 1.1M×512 再 vstack 实测 210s;CLI 每次 ask 是新进程,
    # 语义成为常规路径(R0 修复)后等于每问 3 分钟。改为首次读完落 .npy(+ids.json),后续
    # 进程 np.load(mmap_mode="r") 秒开(实测 6.9s 冷读盘,页缓存热后 <1s)。派生物,可随时
    # 删除重建;不进备份(prune_backups 只认 *.db.bak.* 模式,_db_backup 只走 sqlite backup API)。
    def _cache_paths(self) -> tuple:
        base = str(self.vec_db)
        return Path(base + ".mat.npy"), Path(base + ".ids.json")

    def _cache_key(self) -> list:
        """跨进程缓存指纹:向量库主文件 (mtime_ns, size) + 向量条数。

        故意不含 -wal:每个进程打开 WAL 库都会新建/删除 -wal,其 mtime 每次都变,拿它做键
        缓存永远失效。build 结束 checkpoint 会改主文件 mtime/size → 自然失效。"""
        try:
            st = self.vec_db.stat()
            main = [st.st_mtime_ns, st.st_size]
        except OSError:
            main = [None, None]
        n = int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        return main + [n]

    def _load_matrix_cache(self) -> bool:
        """命中磁盘缓存则 memmap 装入并返回 True;指纹/形状不符或任何异常 → False(走 BLOB)。"""
        mat_p, ids_p = self._cache_paths()
        if not (mat_p.exists() and ids_p.exists()):
            return False
        try:
            meta = json.loads(ids_p.read_text(encoding="utf-8"))
            if meta.get("key") != self._cache_key():
                return False
            ids = meta.get("ids") or []
            m = np.load(str(mat_p), mmap_mode="r")
            if m.ndim != 2 or m.shape[0] != len(ids) or m.shape[1] != self.backend.dim:
                return False
            self._mat, self._ids = m, ids
            return True
        except Exception as e:
            print(f"[semantic] 矩阵缓存读取失败,回退 BLOB 加载({type(e).__name__}: {e})",
                  file=sys.stderr)
            return False

    def _save_matrix_cache(self, from_tmp: "Path | None" = None, rows: int = 0) -> None:
        """落盘:先写 .tmp 再 os.replace(两个进程同时保存也不会互相撕裂);ids 最后写,
        读端先校指纹再校形状,半新半旧组合会被形状校验拒掉。失败只出声不阻断检索。

        from_tmp:_load_matrix 已把数据逐行写进该 .tmp 的 memmap(2026-08-26 省内存路径),
        此处只需 flush + rename,再以只读 memmap 重挂 self._mat(不保留可写映射);
        rows 为真实行数,少于分配行数时(有跳过/并发删除)需按 np.save 重写头部,故走全量落盘。"""
        mat_p, ids_p = self._cache_paths()
        try:
            tmp_m = Path(str(mat_p) + ".tmp")
            if from_tmp is not None and rows == getattr(self._mat, "shape", (0,))[0]:
                # 不 `del self._mat`:若 os.replace 失败(磁盘满/权限/跨盘),属性消失会让本进程
                # 之后每次 search/score 都 AttributeError → 永久空结果(审核 A P1-4)。失败时回退
                # 到内存副本(能装下)或继续用 .tmp memmap 并出声。
                old = self._mat
                old.flush()                           # memmap 已就地写好,零额外内存
                try:
                    os.replace(from_tmp, mat_p)
                    self._mat = np.load(str(mat_p), mmap_mode="r")
                except OSError as e:
                    print(f"[semantic] 矩阵缓存落盘失败(本进程继续用临时矩阵;下次 build 前请重启常驻进程)"
                          f"({type(e).__name__}: {e})", file=sys.stderr)
                    try:
                        self._mat = np.array(old, dtype="float32")   # 拷进内存,不再依赖会被覆写的 .tmp
                    except MemoryError:
                        self._mat = old
                    return
            else:
                with open(tmp_m, "wb") as fh:         # 用文件句柄:np.save 对非 .npy 名会自动加后缀
                    np.save(fh, np.ascontiguousarray(self._mat, dtype="float32"))
                os.replace(tmp_m, mat_p)
            tmp_i = Path(str(ids_p) + ".tmp")
            key = getattr(self, "_key_at_load", None) or self._cache_key()
            self._key_at_load = None
            tmp_i.write_text(json.dumps({"key": key, "ids": self._ids},
                                        ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_i, ids_p)
        except Exception as e:
            print(f"[semantic] 矩阵缓存写入失败(不影响本次检索)({type(e).__name__}: {e})",
                  file=sys.stderr)

    def _load_matrix(self) -> None:
        """加载全部向量到内存并归一化(余弦=点积)。库指纹变了自动重载——
        长驻 web 进程期间外部跑 `tkb semantic build` 不再静默用旧矩阵。
        优先磁盘缓存(P1-D);未命中才从 BLOB 逐行读,读完顺手落盘。"""
        ver = self._db_version()
        if getattr(self, "_mat", None) is not None and ver == self._mat_ver:
            return
        if self._load_matrix_cache():
            self._mat_ver = ver
            return
        # 缓存指纹在**装载开始前**取:装载 210s 期间若 build 又提交了几批,装载后取 COUNT 会记成
        # 更大的条数,写出"key 说 N+k 条、矩阵只有 N 行"的缓存,被下个进程接受(审核 A P2-25)。
        self._key_at_load = self._cache_key()
        # 直接写盘 memmap 再逐行填充(2026-08-26):旧写法 list(全量) → vstack(全量) → m/norms(全量)
        # 同时持有三份副本,190 万×512 float32 时峰值 ~11.6GB,8GB 机器必然换页(回填后必现)。
        # 现在数据直接落到缓存文件的 memmap 上、逐行归一化,常驻内存只有页缓存(可被 OS 回收)。
        n = int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        if not n:
            self._ids, self._mat, self._mat_ver = [], np.zeros((0, 1), dtype="float32"), ver
            return
        dim = self.backend.dim
        mat_p, ids_p = self._cache_paths()
        tmp_m = Path(str(mat_p) + ".tmp")
        ids: list = []
        used_memmap = True
        try:
            arr = np.lib.format.open_memmap(tmp_m, mode="w+", dtype="float32", shape=(n, dim))
        except Exception as e:                       # 无处可写(只读盘/权限)→ 退回内存单副本
            print(f"[semantic] 矩阵缓存不可写,内存构建({type(e).__name__}: {e})", file=sys.stderr)
            arr = np.empty((n, dim), dtype="float32")
            used_memmap = False
        i = 0
        try:
            for fid, blob in self._conn.execute("SELECT fact_id, vec FROM vectors"):
                if i >= n:                           # 并发新增:本轮只收指纹对应的前 n 条
                    break
                v = np.frombuffer(blob, dtype="float32")
                if v.shape[0] != dim:                # 维度不符(换后端/半截 BLOB)→ 跳过,不污染矩阵
                    continue
                nrm = float(np.linalg.norm(v)) or 1.0
                arr[i] = v / nrm
                ids.append(fid)
                i += 1
        except Exception:
            # 读 BLOB 中途异常(disk I/O error 等):别把 3.8GB 的半截 .tmp 留在盘上(审核 A P2-24)
            if used_memmap:
                del arr
                tmp_m.unlink(missing_ok=True)
            raise
        if i < n and used_memmap:
            # 真实行数少于预分配(维度不符被跳过/并发删除):.npy 头部行数必须与 ids 数一致,
            # 否则读端形状校验永远拒收、每次 ask 都退回 BLOB 全量重建。按真实行数分块重写一份。
            tmp2 = Path(str(mat_p) + ".tmp2")
            try:
                arr2 = np.lib.format.open_memmap(tmp2, mode="w+", dtype="float32", shape=(i, dim))
                for s in range(0, i, 50000):
                    e2 = min(s + 50000, i)           # 上界必须显式钳到 i:源 arr 有 n(>i) 行,
                    arr2[s:e2] = arr[s:e2]           # 直接 s:s+50000 会取回 n 行、形状不匹配
                arr2.flush()
                del arr, arr2
                tmp_m.unlink(missing_ok=True)
                tmp_m = tmp2
                arr = np.load(str(tmp_m), mmap_mode="r")
            except Exception as e:
                print(f"[semantic] 截断重写失败,内存兜底({type(e).__name__}: {e})", file=sys.stderr)
                arr = np.array(arr[:i], dtype="float32")
                used_memmap = False
                tmp2.unlink(missing_ok=True)         # 别留半截 .tmp2
                tmp_m.unlink(missing_ok=True)
        elif i < n:
            arr = arr[:i]
        self._mat, self._ids, self._mat_ver = arr, ids, ver
        self._save_matrix_cache(from_tmp=tmp_m if used_memmap else None, rows=i)

    def search(self, query: str, top_k: int = 120) -> list[str]:
        """语义召回 top-k fact_ids(召回字面不匹配但语义相关的事实)。

        失败降级返回 [](ask 回退 LIKE),但必须出声(§2.2)——v0.4 曾把跨线程
        ProgrammingError 无声吞掉,web 端语义召回静默失效无人知。
        """
        try:
            with self._lock:
                self._load_matrix()
                if not self._ids:
                    return []
                sims = self._mat @ self._encode_query(query)
                order = np.argsort(-sims)[:top_k]
                return [self._ids[i] for i in order]
        except Exception as e:
            print(f"[semantic] search 降级为空({type(e).__name__}: {e})", file=sys.stderr)
            return []

    def score(self, query: str, fact_ids: list) -> dict:
        """对给定 fact_ids 返回语义相似度 {fid: 0~1}；缺失/异常返回空(带告警)。"""
        try:
            with self._lock:
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
        except Exception as e:
            print(f"[semantic] score 降级为空({type(e).__name__}: {e})", file=sys.stderr)
            return {}

    def vector_count(self) -> int:
        """已建向量条数(cli status 用,替代裸摸 _conn)。"""
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])

    def close(self) -> None:
        """关闭 sqlite 连接（长驻调用方如 web.py 收尾用；CLI 一次性进程靠退出回收）。"""
        try:
            self._conn.close()
        except Exception:
            pass

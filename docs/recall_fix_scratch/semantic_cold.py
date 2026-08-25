"""语义冷启动拆分计时:后端/模型加载 vs 向量矩阵加载(sqlite BLOB) vs .npy memmap 替代。只读,不写生产库。"""
import time, sys, os
sys.path.insert(0, "src")
import numpy as np
from trading_kb import semantic as S
t = time.time(); be = S._auto_backend("data/facts.db"); print("backend", f"{time.time()-t:.1f}s", flush=True)
idx = S.SemanticIndex("data/facts.db", be)
t = time.time(); idx._encode_query("球硅"); print("encode(model load)", f"{time.time()-t:.1f}s", flush=True)
t = time.time(); idx._load_matrix(); print("load_matrix(sqlite BLOB)", f"{time.time()-t:.1f}s", idx._mat.shape, flush=True)
t = time.time(); idx.search("球硅 HBM 先进封装 需求", top_k=120); print("search", f"{time.time()-t:.2f}s")
p = "/tmp/tkb_mat_probe.npy"
t = time.time(); np.save(p, idx._mat); print("np.save", f"{time.time()-t:.1f}s", os.path.getsize(p)/1e9, "GB")
t = time.time(); m = np.load(p, mmap_mode="r"); s = m @ idx._encode_query("球硅 HBM 先进封装 需求"); print("memmap load+matmul", f"{time.time()-t:.2f}s")

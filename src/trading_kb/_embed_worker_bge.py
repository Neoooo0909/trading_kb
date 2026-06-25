"""隔离 venv(.venv-embed, py3.11)中的 bge-small-zh-v1.5 编码 worker（ONNX，无 torch）。

主进程经 subprocess 调用：argv[1]=模型目录(含 model.onnx + tokenizer.json + config.json)，
stdin=每行一个 JSON 字符串(文本)，stdout=np.save 的 float32 (N×D) 已 L2 归一化向量。
只依赖 onnxruntime + tokenizers + numpy，把 contextual embedding 依赖隔离在 venv，
不污染零依赖核心、3.14 主进程照常跑。

bge 句向量约定：CLS pooling（取 last_hidden_state 第 0 个 token）后 L2 归一化。
query 端的检索前缀由主进程在编码前拼好，本 worker 对文本一视同仁、保持无状态。
"""
import io
import json
import os
import sys

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

_MAX_LEN = 512
_BATCH = 64


def _hidden_size(model_dir: str, default: int = 512) -> int:
    """从 config.json 读 hidden_size（决定空输入时返回向量的维度）。"""
    try:
        with open(os.path.join(model_dir, "config.json"), encoding="utf-8") as f:
            return int(json.load(f).get("hidden_size", default))
    except Exception:
        return default


def _build_tokenizer(model_dir: str) -> Tokenizer:
    """加载 BERT 风格 tokenizer，开启截断(512)与 batch padding(pad_id=0=[PAD])。"""
    tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
    tok.enable_truncation(max_length=_MAX_LEN)
    tok.enable_padding(pad_id=0, pad_token="[PAD]")
    return tok


def _session(model_dir: str) -> ort.InferenceSession:
    """建 ONNX 推理会话。默认 CPU(稳)；TKB_ORT_PROVIDER=coreml 时优先 CoreML，失败自动回退 CPU。"""
    prov = os.environ.get("TKB_ORT_PROVIDER", "cpu").lower()
    providers = (["CoreMLExecutionProvider", "CPUExecutionProvider"]
                 if prov == "coreml" else ["CPUExecutionProvider"])
    return ort.InferenceSession(os.path.join(model_dir, "model.onnx"), providers=providers)


def _encode(sess: ort.InferenceSession, tok: Tokenizer, texts: list[str]) -> np.ndarray:
    """对一批文本做 CLS pooling + L2 归一化，返回 (len×D) float32。"""
    want = {i.name for i in sess.get_inputs()}            # 模型实际接受的输入名
    outs_all = []
    for i in range(0, len(texts), _BATCH):
        chunk = texts[i:i + _BATCH]
        encs = tok.encode_batch(chunk)
        ids = np.asarray([e.ids for e in encs], dtype=np.int64)
        attn = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": attn,
                 "token_type_ids": np.zeros_like(ids)}
        feeds = {k: v for k, v in feeds.items() if k in want}
        res = sess.run(None, feeds)
        arr3 = next((o for o in res if getattr(o, "ndim", 0) == 3), None)
        emb = arr3[:, 0] if arr3 is not None else res[0]   # CLS pooling 或已池化输出
        emb = np.asarray(emb, dtype="float32")
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        outs_all.append(emb / norms)
    return np.vstack(outs_all)


def main() -> None:
    model_dir = sys.argv[1]
    texts = [json.loads(ln) for ln in sys.stdin if ln.strip()]
    if not texts:
        vecs = np.zeros((0, _hidden_size(model_dir)), dtype="float32")
    else:
        vecs = _encode(_session(model_dir), _build_tokenizer(model_dir), texts)
    # np.save 需可 seek 的流；stdout 管道不可 seek，先写 BytesIO 再整体 dump
    buf = io.BytesIO()
    np.save(buf, np.asarray(vecs, dtype="float32"))
    sys.stdout.buffer.write(buf.getvalue())


if __name__ == "__main__":
    main()

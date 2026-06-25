"""隔离 venv(.venv-embed, py3.11 + model2vec 静态向量)中的编码 worker。

主进程经 subprocess 调用:argv[1]=模型目录，stdin=每行一个 JSON 字符串(文本)，
stdout=np.save 的 float32 (N×D) 向量。不 import trading_kb，只依赖 model2vec + numpy，
从而把 embedding 依赖完全隔离在 venv 里、不污染零依赖的核心。
"""
import io
import json
import sys

import numpy as np
from model2vec import StaticModel


def main() -> None:
    model_dir = sys.argv[1]
    texts = [json.loads(ln) for ln in sys.stdin if ln.strip()]
    model = StaticModel.from_pretrained(model_dir)
    vecs = model.encode(texts) if texts else np.zeros((0, 256), dtype="float32")
    # np.save 需要可 seek 的流；stdout 管道不可 seek，故先写 BytesIO 再整体 dump
    buf = io.BytesIO()
    np.save(buf, np.asarray(vecs, dtype="float32"))
    sys.stdout.buffer.write(buf.getvalue())


if __name__ == "__main__":
    main()

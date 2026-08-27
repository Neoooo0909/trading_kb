"""JSONL 读取的唯一实现(2026-08-27,历史问题 #9)。

`read_text().splitlines()` 会把 U+2028/U+2029/\\x1c-\\x1e 等 Unicode 行分隔符当换行,
把一行 JSON 切成两半:带 `except: continue` 的读法静默丢记录(29 帖曾永久丢失),
不带的直接 JSONDecodeError 让夜跑 apply 阶段崩。LLM 自由文本(reason/subject)里出现
这类字符并不罕见。这里**只认 \\n**(按文件行迭代),坏行出声计数、不吞。
scripts/ 与 ~/ZSXQ/kb_adapter 共用本模块(kb_adapter 本就 import trading_kb)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator


def iter_jsonl(path, *, strict: bool = False, tag: str = "") -> Iterator[dict]:
    """逐行 yield JSON 对象。文件不存在 → 空迭代。坏行:strict=True 抛 ValueError(带行号),
    否则 stderr 出声一行并跳过(每文件只报前 3 条,末尾汇总计数)。"""
    p = Path(path)
    if not p.exists():
        return
    bad = 0
    with open(p, encoding="utf-8", newline="\n") as fh:      # newline="\n":只把 \n 当行尾
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                bad += 1
                if strict:
                    raise ValueError(f"{p}:{lineno} 坏 JSON 行: {e}") from e
                if bad <= 3:
                    print(f"[jsonl] {tag or p.name}:{lineno} 坏行已跳过({e})", file=sys.stderr)
                continue
            yield obj
    if bad > 3:
        print(f"[jsonl] {tag or p.name}: 共跳过 {bad} 条坏行", file=sys.stderr)


def read_jsonl(path, **kw) -> list:
    """一次读完(小文件用)。"""
    return list(iter_jsonl(path, **kw))


def iter_lines(path) -> list:
    """按 **只认 \\n** 的方式读整个文件返回行列表(去掉行尾 \\n),是 `read_text().splitlines()`
    的直接替代——后者会把 U+2028 等当行尾把 JSON 行切碎。文件不存在返回 []。
    调用方保留自己的 json.loads/except 逻辑,只是不再被假换行切碎。"""
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8", newline="\n") as fh:
        return [ln.rstrip("\n") for ln in fh]

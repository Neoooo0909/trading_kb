"""假设追踪层(P1，借鉴 ai-workspace-hub 的 hypothesis 理念)。

把"待验证结论"做成持续累积证据的活假设:一假设一 H*.md(人可读可编辑、零依赖)。
证据按 for/against + 成色累积，自动估算置信度;复盘后 resolve 出结论。
治"一次性分析无持续追踪"的短板——如"精智达 2026 净利率能否兑现研报假设的 25%"。

同文件附 friction-log(append-only 摩擦日志)，驱动系统改进。
"""
from __future__ import annotations

import re
from datetime import date as _Date
from pathlib import Path

_LEVEL_W = {"A": 4, "B+": 3, "B": 2, "C": 1, "D": 0}   # 证据成色权重(与 facts 口径一致)


def _today() -> str:
    """当天 ISO 日期。"""
    return _Date.today().isoformat()


class HypothesisStore:
    """活假设账本:hypotheses/ 下一假设一 H*.md(frontmatter + 证据日志 + 结论)。"""

    def __init__(self, base_dir):
        self.root = Path(base_dir)
        self.dir = self.root / "hypotheses"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── 增/改 ────────────────────────────────────────────────────────────
    def new(self, title: str, ticker: str = "", statement: str = "") -> str:
        """新建假设，返回分配的 id(H001…)。"""
        hid = self._next_id()
        fm = {"id": hid, "title": title, "ticker": ticker,
              "status": "open", "confidence": "0.50",
              "created": _today(), "updated": _today()}
        body = (f"## 假设\n{statement or title}\n\n"
                f"## 证据日志\n"
                f"（追加: ./tkb hyp evidence {hid} \"…\" --side for|against --grade B）\n\n"
                f"## 结论\n（未定: ./tkb hyp resolve {hid} <confirmed|refuted|partial> \"…\"）\n")
        self._write(hid, fm, body)
        return hid

    def add_evidence(self, hid: str, text: str, side: str,
                     grade: str = "C", date: str = "") -> float:
        """追加一条证据(side=for/against，grade=成色)，刷新置信度并返回。"""
        if side not in ("for", "against"):
            raise ValueError("side 必须是 for 或 against")
        fm, body = self._read(hid)
        line = f"- [{date or _today()}][{side}][{grade}] {text}"
        body = self._append_to_section(body, "证据日志", line)
        conf = self._confidence(body)
        fm["updated"] = _today()
        fm["confidence"] = f"{conf:.2f}"
        self._write(hid, fm, body)
        return conf

    def resolve(self, hid: str, verdict: str, conclusion: str) -> None:
        """结案(verdict=confirmed/refuted/partial)，写结论段。"""
        if verdict not in ("confirmed", "refuted", "partial"):
            raise ValueError("verdict 必须是 confirmed/refuted/partial")
        fm, body = self._read(hid)
        fm["status"] = verdict
        fm["updated"] = _today()
        body = self._set_section(body, "结论", f"[{verdict}] {conclusion}")
        self._write(hid, fm, body)

    # ── 查 ──────────────────────────────────────────────────────────────
    def exists(self, hid: str) -> bool:
        """假设是否存在（供调用方在 show/evidence/resolve 前做友好校验）。"""
        return bool(hid) and self._path(hid).exists()

    def get(self, hid: str) -> str:
        """返回 H*.md 全文。"""
        return self._path(hid).read_text(encoding="utf-8")

    def list_all(self) -> list[dict]:
        """列出所有假设的 frontmatter 摘要(含证据条数)。"""
        out = []
        for p in sorted(self.dir.glob("H*.md")):
            fm, body = self._parse(p.read_text(encoding="utf-8"))
            fm["n_evidence"] = len(re.findall(r"(?m)^- \[", body))
            out.append(fm)
        return out

    # ── 置信度 ──────────────────────────────────────────────────────────
    @staticmethod
    def _confidence(body: str) -> float:
        """按 for/against 证据的成色加权估置信度(0~1，0.5 为无证据先验)。"""
        forw = againstw = 0
        for side, grade in re.findall(r"(?m)^- \[[^\]]*\]\[(for|against)\]\[([AB+CD]+)\]", body):
            w = _LEVEL_W.get(grade, 1) + 1            # +1 让 D 也有最小权重
            if side == "for":
                forw += w
            else:
                againstw += w
        if forw + againstw == 0:
            return 0.5
        return forw / (forw + againstw)

    # ── 内部:文件/frontmatter/section ───────────────────────────────────
    def _next_id(self) -> str:
        nums = [int(m.group(1)) for p in self.dir.glob("H*.md")
                if (m := re.match(r"H(\d+)", p.stem))]
        return f"H{(max(nums) + 1) if nums else 1:03d}"

    def _path(self, hid: str) -> Path:
        return self.dir / f"{hid}.md"

    @staticmethod
    def _flat(v) -> str:
        """frontmatter 值单行化：换行会被 _parse 误拆成额外 key，故折叠为空格。"""
        return " ".join(str(v).split())

    def _write(self, hid: str, fm: dict, body: str) -> None:
        fm_txt = "\n".join(f"{k}: {self._flat(v)}" for k, v in fm.items())
        self._path(hid).write_text(f"---\n{fm_txt}\n---\n\n{body}", encoding="utf-8")

    def _read(self, hid: str):
        return self._parse(self._path(hid).read_text(encoding="utf-8"))

    @staticmethod
    def _parse(text: str):
        """拆 frontmatter(--- 包裹的 key: value)与正文。"""
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        if not m:
            return {}, text
        fm = {}
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        return fm, m.group(2).lstrip("\n")

    @staticmethod
    def _section_range(lines: list[str], title: str):
        """返回 ## <title> 段的 (标题行号, 段尾行号)；不存在返回 (None, None)。"""
        target = f"## {title}"
        start = next((i for i, ln in enumerate(lines) if ln.strip() == target), None)
        if start is None:
            return None, None
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].strip().startswith("## "):
                end = j
                break
        return start, end

    @classmethod
    def _append_to_section(cls, body: str, title: str, line: str) -> str:
        """在 ## <title> 段尾(下一个 ## 前、跳过尾部空行)追加一行。"""
        lines = body.splitlines()
        start, end = cls._section_range(lines, title)
        if start is None:
            return body
        insert = end
        while insert > start + 1 and lines[insert - 1].strip() == "":
            insert -= 1
        lines.insert(insert, line)
        return "\n".join(lines) + "\n"

    @classmethod
    def _set_section(cls, body: str, title: str, text: str) -> str:
        """替换 ## <title> 段的内容。"""
        lines = body.splitlines()
        start, end = cls._section_range(lines, title)
        if start is None:
            return body
        return "\n".join(lines[:start + 1] + ["", text, ""] + lines[end:]) + "\n"


def append_friction(base_dir, text: str) -> None:
    """追加一条摩擦记录到 friction-log.md(append-only，驱动系统改进)。"""
    p = Path(base_dir) / "friction-log.md"
    if not p.exists():
        p.write_text("# Friction Log\n\n工具/流程踩坑记录，集中沉淀以驱动系统改进。\n\n",
                     encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- [{_today()}] {text}\n")

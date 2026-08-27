"""日期口径工具(2026-08-26 valid_at 修复,见 docs/VALID_AT_FIX_PLAN_20260826.md)。

四件事:
  clean_date      入库闸口——valid_at 只接受 YYYY-MM-DD、日历合法、[2000, 明天],否则 ''(=未知)。
  date_from_name  文件名/标题推日期:8 位 YYYYMMDD → 中文年月日 → 6 位 YYMMDD,逐候选日历校验。
  pdf_date        PDF 头尾 8KB 的 /CreationDate、/ModDate;模板/批量归档日黑名单;两者相差>30天取 ModDate。
  ZsxqPostIndex   星球帖子附件索引 (分组,文件名)→帖子日期;多帖极差≤3天取最早,>3天视为歧义。
校准数据(全量扫描):帖日期 vs 文件名日期 ≤3 天 94.5%;CreationDate vs 卡片日期 ≤7 天 99%。
"""
from __future__ import annotations

import datetime
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_P8 = re.compile(r"(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])")
_PCN = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_P6 = re.compile(r"(?<!\d)(2[0-9])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_PDF_C = re.compile(rb"/CreationDate\s*\(D:(\d{8})")
_PDF_M = re.compile(rb"/ModDate\s*\(D:(\d{8})")
MIN_DATE = datetime.date(2000, 1, 1)
PDF_BLACKLIST_PATH = Path.home() / "ZSXQ" / "kb_adapter" / "pdf_date_blacklist.json"


def _valid(y, m, d, today: Optional[datetime.date] = None) -> Optional[datetime.date]:
    """日历合法且落在 [2000-01-01, 今天+1] 才算日期,否则 None。"""
    try:
        dt = datetime.date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None
    limit = (today or datetime.date.today()) + datetime.timedelta(days=1)
    return dt if MIN_DATE <= dt <= limit else None


def clean_date(s, today: Optional[datetime.date] = None) -> str:
    """valid_at 入库闸口:`2026-84-17`、`2025-00-82`、未来日、非 ISO 形状一律返回 ''。"""
    s = (str(s or "")).strip()[:10]
    m = _ISO.match(s)
    if not m:
        return ""
    dt = _valid(*m.groups(), today=today)
    return dt.isoformat() if dt else ""


def date_from_name(name: str, today: Optional[datetime.date] = None) -> str:
    """文件名/标题推日期。8 位优先(`20250409`/`2025-04-09`),再中文年月日,最后 6 位 `250429`;
    每个候选做日历校验、拒未来日,取首个合法者——"2026-2028展望"不会被切成 2026-20-28。"""
    for pat, prefix in ((_P8, ""), (_PCN, ""), (_P6, "20")):
        for m in pat.finditer(name or ""):
            dt = _valid(prefix + m.group(1), m.group(2), m.group(3), today=today)
            if dt:
                return dt.isoformat()
    return ""


def pdf_raw_dates(path, today: Optional[datetime.date] = None) -> tuple[str, str]:
    """只读 PDF 头尾各 8KB,返回 (CreationDate, ModDate),取不到为 ''。不解析全文。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(8192)
            fh.seek(max(0, size - 8192))
            tail = fh.read(8192)
    except OSError:
        return "", ""
    out = []
    for pat in (_PDF_C, _PDF_M):
        m = pat.search(head + tail)
        dt = _valid(m.group(1)[:4], m.group(1)[4:6], m.group(1)[6:8], today=today) if m else None
        out.append(dt.isoformat() if dt else "")
    return out[0], out[1]


def pdf_date(path, blacklist: Iterable[str] = (), today: Optional[datetime.date] = None) -> tuple[str, str]:
    """PDF 元数据推日期 → (date, source),source ∈ {pdf_creation, pdf_mod, ''}。
    黑名单 = 模板日/批量归档日(同一 CreationDate 全库出现 ≥20 次,如花旗模板 2023-09-14、
    SemiAnalysis 批量打印 2023-09-14~16);命中则不用 CreationDate。ModDate 比 CreationDate 晚
    >30 天时说明文件被改过,取 ModDate。"""
    c, m = pdf_raw_dates(path, today=today)
    bl = set(blacklist)
    if c and c in bl:
        c = ""
    if c and m and abs((datetime.date.fromisoformat(m) - datetime.date.fromisoformat(c)).days) > 30:
        return m, "pdf_mod"
    if c:
        return c, "pdf_creation"
    if m and m not in bl:
        return m, "pdf_mod"
    return "", ""


def load_pdf_blacklist(path: Path = PDF_BLACKLIST_PATH) -> set[str]:
    """读黑名单文件(由 scripts/backfill_valid_at.py 全库统计后写入);没有就空集。"""
    try:
        return set(json.loads(Path(path).read_text(encoding="utf-8")).get("dates", []))
    except (OSError, ValueError, AttributeError):
        return set()


class ZsxqPostIndex:
    """星球帖子附件索引:posts.jsonl 的 files[].name → create_time[:10]。

    同名文件全库有 1,115 个重复(`周度复盘_8.pdf` 之类),所以优先按 (分组, 文件名) 查,
    查不到再退到裸文件名;多帖日期极差 ≤3 天取最早(最接近发布),>3 天视为歧义返回 ''。"""

    def __init__(self, root: Path = Path.home() / "ZSXQ" / "download"):
        self.root = Path(root)
        self._by_group: Optional[dict] = None
        self._by_name: Optional[dict] = None

    def _build(self) -> None:
        by_group, by_name = defaultdict(set), defaultdict(set)
        for pj in sorted(self.root.glob("*/帖子/posts.jsonl")):
            grp = pj.parent.parent.name
            try:
                fh = open(pj, encoding="utf-8")
            except OSError:
                continue
            with fh:
                for line in fh:
                    try:
                        p = json.loads(line)
                    except ValueError:
                        continue
                    ct = clean_date((p.get("create_time") or "")[:10])
                    if not ct:
                        continue
                    for f in p.get("files") or []:
                        nm = (f.get("name") or "").strip() if isinstance(f, dict) else ""
                        if nm:
                            by_group[(grp, nm)].add(ct)
                            by_name[nm].add(ct)
        self._by_group, self._by_name = dict(by_group), dict(by_name)

    @staticmethod
    def pick(dates: Iterable[str]) -> str:
        """多帖取最早;极差 >3 天判歧义返回 ''。"""
        ds = sorted(set(dates))
        if not ds:
            return ""
        span = (datetime.date.fromisoformat(ds[-1]) - datetime.date.fromisoformat(ds[0])).days
        return ds[0] if span <= 3 else ""

    def group_of(self, path) -> Optional[str]:
        """文件若在 download/<分组>/… 下,返回分组名。"""
        try:
            rel = Path(path).resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return None
        return rel.parts[0] if rel.parts else None

    def lookup(self, name: str, group: Optional[str] = None) -> str:
        if self._by_group is None:
            self._build()
        name = (name or "").strip()
        if group and (group, name) in self._by_group:
            return self.pick(self._by_group[(group, name)])
        if group is None and name in self._by_name:
            return self.pick(self._by_name[name])
        return ""


def resolve_date(path, post_index: Optional[ZsxqPostIndex] = None,
                 blacklist: Iterable[str] = (), today: Optional[datetime.date] = None) -> tuple[str, str]:
    """卡片日期统一解析:文件名 → 星球帖子(同组优先) → PDF 元数据。返回 (date, source)。"""
    p = Path(path)
    d = date_from_name(p.name, today=today)
    if d:
        return d, "filename"
    if post_index is not None:
        grp = post_index.group_of(p)
        d = post_index.lookup(p.name, grp) if grp else post_index.lookup(p.name, None)
        if d:
            return d, "zsxq_post"
    if p.suffix.lower() == ".pdf" and p.exists():
        d, src = pdf_date(p, blacklist=blacklist, today=today)
        if d:
            return d, src
    return "", ""

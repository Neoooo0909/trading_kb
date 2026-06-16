"""A股公告抓取(免费权威源)+ orgId精确查询 + 正文提取 + 分类。

优先级:
  ① 巨潮 cninfo —— 证监会法定披露平台,沪深全量,免费无额度(主)
  ② 上交所 query.sse.com.cn —— 沪市票兜底(需 Referer)

能力:
  - orgId 精确查询:code→orgId(cninfo szse_stock.json,本地缓存),避重名(akshare 等同款做法)
  - 公告分类:标题关键词 → 18 大类(定期报告/业绩预告/减持/中标/重组/分红/...)
  - 正文提取:下载 PDF(static.cninfo.com.cn)+ pdftotext 全文,供深度质疑核对

风控实测:连发无拦截,高频 456 退避重试;无验证码。直连不走代理。
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连不走代理


@dataclass
class Announcement:
    title: str
    date: str
    url: str               # PDF 完整 URL
    source: str            # cninfo / sse
    category: str = "其他"  # 公告大类(分类)
    code: str = ""


# ── 公告分类:标题关键词 → 大类(有序,先匹配先得;粗到细排序)─────────────
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("业绩预告/快报", ["业绩预告", "业绩快报", "预增", "预减", "预盈", "预亏", "扭亏"]),
    ("定期报告", ["年度报告", "半年度报告", "第一季度报告", "第三季度报告", "季度报告", "年报", "中报"]),
    ("股东增减持", ["减持", "增持", "持股变动", "权益变动", "股份减少", "股份增加"]),
    ("重大合同/中标", ["中标", "重大合同", "签订", "框架协议", "重大订单", "采购合同"]),
    ("重组并购", ["重组", "收购", "并购", "资产购买", "资产出售", "吸收合并", "重大资产"]),
    ("股权激励/员工持股", ["股权激励", "员工持股", "限制性股票", "股票期权", "激励计划"]),
    ("融资/再融资", ["定向增发", "非公开发行", "可转债", "配股", "发行股份", "募集资金", "公开发行"]),
    ("分红送转", ["利润分配", "分红", "派息", "送转", "现金分红", "权益分派"]),
    ("股份回购", ["回购"]),
    ("对外投资", ["对外投资", "设立子公司", "投资建设", "投资设立", "增资"]),
    ("关联交易", ["关联交易"]),
    ("担保", ["担保"]),
    ("诉讼/风险/处罚", ["诉讼", "仲裁", "风险提示", "处罚", "立案", "被调查", "监管"]),
    ("澄清/媒体回应", ["澄清", "媒体报道", "传闻", "说明公告"]),
    ("停复牌", ["停牌", "复牌"]),
    ("人事/治理", ["董事", "监事", "高级管理", "辞职", "聘任", "选举", "换届"]),
    ("股东大会", ["股东大会", "股东会", "会议决议", "决议公告", "会议通知"]),
]


def classify_title(title: str) -> str:
    """公告标题 → 大类(分类)。无命中归"其他"。"""
    t = title or ""
    for cat, kws in _CATEGORY_RULES:
        if any(kw in t for kw in kws):
            return cat
    return "其他"


# ── orgId 精确查询:code → orgId(本地缓存全市场映射)────────────────────
_STOCK_MAP: Optional[dict[str, dict]] = None


def _stock_cache_path() -> Path:
    return config.DATA_DIR / "cninfo_stocks.json"


def _load_stock_map() -> dict[str, dict]:
    """加载 code→{orgId,name,plate} 映射;优先本地缓存,缺失则下载(7天过期)。"""
    global _STOCK_MAP
    if _STOCK_MAP is not None:
        return _STOCK_MAP
    cache = _stock_cache_path()
    raw = None
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 7 * 86400:
        try:
            raw = json.loads(cache.read_text())
        except (json.JSONDecodeError, OSError):
            raw = None
    if raw is None:
        raw = _download_stock_list()
        if raw:
            try:
                config.ensure_data_dir()
                cache.write_text(json.dumps(raw, ensure_ascii=False))
            except OSError:
                pass
    _STOCK_MAP = raw or {}
    return _STOCK_MAP


def _download_stock_list() -> dict[str, dict]:
    """下载 cninfo 全市场股票表(含 orgId)。szse_stock.json 实为全市场全量。"""
    body = _http("http://www.cninfo.com.cn/new/data/szse_stock.json",
                 headers={"User-Agent": _UA})
    if not body:
        return {}
    try:
        lst = json.loads(body).get("stockList") or []
    except (json.JSONDecodeError, TypeError):
        return {}
    out = {}
    for s in lst:
        code = s.get("code")
        if code:
            out[code] = {"orgId": s.get("orgId", ""), "name": s.get("zwjc", "")}
    return out


def resolve_orgid(code: str) -> Optional[str]:
    """code → orgId(精确查询用)。查不到返回 None,降级用公司名 searchkey。"""
    d = _digits(code)
    info = _load_stock_map().get(d)
    return info.get("orgId") if info else None


# ── HTTP(直连,失败返回 None 不抛)──────────────────────────────────────
def _http(url: str, *, data: bytes | None = None, headers: dict, timeout: int = 15) -> Optional[str]:
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            if r.status == 200:
                return r.read().decode("utf-8", "ignore")
    except Exception:
        return None
    return None


# ── ① 巨潮(主,支持 orgId 精确查询)──────────────────────────────────────
def query_cninfo(keyword: str = "", code: str = "", plate: str = "",
                 page_size: int = 10, se_date: str = "", retries: int = 3) -> list[Announcement]:
    """查巨潮公告。给 code 时优先 orgId 精确查询;否则用 keyword 公司名模糊查。"""
    plate = plate or _plate_of(code)
    column = {"sh": "sse", "sz": "szse", "bj": "bj"}.get(plate, "szse")
    stock_param = ""
    orgid = resolve_orgid(code) if code else None
    if code and orgid:
        stock_param = f"{_digits(code)},{orgid}"     # 精确:code,orgId
    body = urllib.parse.urlencode({
        "pageNum": 1, "pageSize": page_size, "column": column,
        "tabName": "fulltext", "searchkey": "" if stock_param else keyword,
        "stock": stock_param, "plate": plate, "seDate": se_date, "category": "",
    }).encode()
    headers = {"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded",
               "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"}
    for i in range(retries):
        resp = _http("http://www.cninfo.com.cn/new/hisAnnouncement/query", data=body, headers=headers)
        if resp:
            try:
                anns = json.loads(resp).get("announcements") or []
            except (json.JSONDecodeError, TypeError):
                return []
            return [Announcement(
                title=a.get("announcementTitle", ""),
                date=_ts_to_date(a.get("announcementTime")),
                url="http://static.cninfo.com.cn/" + (a.get("adjunctUrl") or ""),
                source="cninfo", category=classify_title(a.get("announcementTitle", "")),
                code=_digits(code)) for a in anns]
        time.sleep(0.5 * (i + 1))
    return []


# ── ② 上交所(沪市兜底)────────────────────────────────────────────────
def query_sse(code: str, page_size: int = 10) -> list[Announcement]:
    url = ("http://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do"
           f"?isPagination=true&pageHelp.pageSize={page_size}&pageHelp.pageNo=1&SECURITY_CODE={code}")
    resp = _http(url, headers={"User-Agent": _UA, "Referer": "http://www.sse.com.cn/"})
    if not resp:
        return []
    try:
        data = json.loads(resp)
        rows = data.get("pageHelp", {}).get("data") or data.get("result") or []
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for r in rows:
        u = r.get("URL") or ""
        if u and not u.startswith("http"):
            u = "http://www.sse.com.cn" + u
        title = r.get("TITLE", "") or r.get("BULLETIN_TITLE", "")
        out.append(Announcement(title=title, date=r.get("SSEDATE", "") or r.get("BULLETIN_DATE", ""),
                                url=u, source="sse", category=classify_title(title), code=_digits(code)))
    return out


# ── 统一入口 ──────────────────────────────────────────────────────────────
def fetch_announcements(name: str = "", code: str = "", limit: int = 10,
                        category: str = "") -> list[Announcement]:
    """抓公告(已分类)。巨潮主 + 沪市兜底上交所;category 非空时按大类过滤。"""
    plate = _plate_of(code)
    anns = query_cninfo(keyword=name, code=code, plate=plate, page_size=limit)
    if not anns and plate == "sh" and code:
        anns = query_sse(_digits(code))
    if category:
        anns = [a for a in anns if a.category == category]
    return anns[:limit]


def has_announcement(name: str = "", code: str = "", keyword: str = "",
                     category: str = "") -> bool:
    """是否存在(含关键词/指定大类的)公告 —— 供 web_enrich 验成色。"""
    anns = fetch_announcements(name, code, limit=10, category=category)
    if not keyword:
        return bool(anns)
    return any(keyword in a.title for a in anns)


# ── ③ 正文提取(下载 PDF + pdftotext)──────────────────────────────────
def fetch_with_text(name: str = "", code: str = "", limit: int = 3,
                    category: str = "") -> list[dict]:
    """抓公告并提取正文(供深度质疑核对披露)。返回 [{title,date,category,url,text}]。"""
    out = []
    for a in fetch_announcements(name, code, limit=limit, category=category):
        text = ""
        pdf = _download_pdf(a)
        if pdf:
            text = extract_text(pdf)
        out.append({"title": a.title, "date": a.date, "category": a.category,
                    "url": a.url, "text_chars": len(text), "text": text})
    return out


def _download_pdf(a: Announcement) -> Optional[Path]:
    """下载公告 PDF 到缓存目录;失败返回 None。"""
    if not a.url.lower().endswith((".pdf",)):
        return None
    cache = config.DATA_DIR / "ann_pdf"
    cache.mkdir(parents=True, exist_ok=True)
    fp = cache / (str(abs(hash(a.url)))[:16] + ".pdf")
    if fp.exists() and fp.stat().st_size > 0:
        return fp
    try:
        req = urllib.request.Request(a.url, headers={"User-Agent": _UA})
        with _OPENER.open(req, timeout=30) as r:
            if r.status == 200:
                fp.write_bytes(r.read())
                return fp
    except Exception:
        return None
    return None


def extract_text(pdf_path: Path) -> str:
    """pdftotext -layout 提取全文(与 report_lab 同工具)。失败返回空串。"""
    try:
        r = subprocess.run(["pdftotext", "-layout", str(pdf_path), "-"],
                           capture_output=True, timeout=60)
        return r.stdout.decode("utf-8", "ignore") if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


# ── 辅助 ──────────────────────────────────────────────────────────────────
def _plate_of(code: str) -> str:
    d = _digits(code)
    if not d:
        return ""
    if d[0] in ("6", "9"):
        return "sh"
    if d[0] in ("0", "3", "2"):
        return "sz"
    if d[0] in ("8", "4"):
        return "bj"
    return ""


def _digits(code: str) -> str:
    return "".join(c for c in (code or "") if c.isdigit())


def _ts_to_date(ts) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts) / 1000))
    except (ValueError, TypeError, OSError):
        return ""

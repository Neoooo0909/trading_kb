"""valid_at 修复(2026-08-26):日期闸口、文件名/PDF/星球帖子推断、ingested_at 迁移、存量修复脚本核心。"""
import datetime
import importlib.util
import json
import os
from pathlib import Path

from trading_kb.dates import (ZsxqPostIndex, clean_date, date_from_name, pdf_date, pdf_raw_dates,
                              resolve_date)
from trading_kb.facts_store import FactsStore
from trading_kb.models import Fact

TODAY = datetime.date(2026, 8, 26)
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_valid_at.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_valid_at", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_clean_date_gate():
    assert clean_date("2026-08-20", TODAY) == "2026-08-20"
    assert clean_date("2026-08-27", TODAY) == "2026-08-27"        # 明天允许(时区容差)
    assert clean_date("2026-08-28", TODAY) == ""                  # 未来
    assert clean_date("2026-84-17", TODAY) == ""                  # 日历非法
    assert clean_date("2025-00-82", TODAY) == ""
    assert clean_date("1999-12-31", TODAY) == ""                  # 早于 2000
    assert clean_date("2026-08-20T10:00:00", TODAY) == "2026-08-20"
    assert clean_date(None, TODAY) == "" and clean_date("", TODAY) == ""


def test_date_from_name_variants():
    assert date_from_name("路维光电…交流250429_原文.docx", TODAY) == "2025-04-29"
    assert date_from_name("20250409-花旗-立讯精密电话会.pdf", TODAY) == "2025-04-09"
    assert date_from_name("2026-2028展望.pdf", TODAY) == ""                 # 不能切成 2026-20-28
    assert date_from_name("中信建投-医药行业周度复盘_8.pdf", TODAY) == ""
    assert date_from_name("宏观周报2026年8月20日.pdf", TODAY) == "2026-08-20"
    assert date_from_name("纪要261231.pdf", TODAY) == ""                    # 6 位但未来


def _fake_pdf(path: Path, creation: str, mod: str = "") -> None:
    body = b"%PDF-1.4\n" + b"x" * 100 + f"<</CreationDate(D:{creation}120000)".encode()
    if mod:
        body += f"/ModDate(D:{mod}120000)".encode()
    body += b">>\n%%EOF"
    path.write_bytes(body)


def test_pdf_date_rules(tmp_path):
    p = tmp_path / "a.pdf"
    _fake_pdf(p, "20260301")
    assert pdf_raw_dates(p, TODAY) == ("2026-03-01", "")
    assert pdf_date(p, today=TODAY) == ("2026-03-01", "pdf_creation")
    # 模板日黑名单 → 不用 CreationDate;有 ModDate 则退到 ModDate
    _fake_pdf(p, "20230914", "20260201")
    assert pdf_date(p, blacklist={"2023-09-14"}, today=TODAY) == ("2026-02-01", "pdf_mod")
    # 两者相差 >30 天取 ModDate(文件被改过)
    _fake_pdf(p, "20250924", "20260624")
    assert pdf_date(p, today=TODAY) == ("2026-06-24", "pdf_mod")
    assert pdf_date(tmp_path / "missing.pdf", today=TODAY) == ("", "")


def test_zsxq_post_index(tmp_path):
    root = tmp_path / "download"
    for grp, rows in {"A群": [("2026-05-20T10:00:00.000+0800", "同名.pdf"), ("2026-05-21T10:00:00.000+0800", "同名.pdf"),
                              ("2026-04-01T09:00:00.000+0800", "独有.pdf")],
                      "B群": [("2026-07-01T10:00:00.000+0800", "同名.pdf")]}.items():
        d = root / grp / "帖子"; d.mkdir(parents=True)
        with open(d / "posts.jsonl", "w", encoding="utf-8") as fh:
            for ct, nm in rows:
                fh.write(json.dumps({"create_time": ct, "files": [{"name": nm}]}, ensure_ascii=False) + "\n")
    idx = ZsxqPostIndex(root)
    assert idx.lookup("独有.pdf", "A群") == "2026-04-01"
    assert idx.lookup("同名.pdf", "A群") == "2026-05-20"          # 同组多帖极差 1 天 → 最早
    assert idx.lookup("同名.pdf", None) == ""                     # 裸名跨组极差 42 天 → 歧义不填
    assert idx.lookup("没有.pdf", "A群") == ""
    f = root / "A群" / "文档图片" / "files" / "独有.pdf"
    assert idx.group_of(f) == "A群" and idx.group_of(tmp_path / "x.pdf") is None
    f.parent.mkdir(parents=True); f.write_bytes(b"%PDF")
    assert resolve_date(f, post_index=idx, today=TODAY) == ("2026-04-01", "zsxq_post")
    g = root / "A群" / "文档图片" / "files" / "纪要250101.pdf"; g.write_bytes(b"%PDF")
    assert resolve_date(g, post_index=idx, today=TODAY) == ("2025-01-01", "filename")


def test_store_ingested_at_and_gate(tmp_path):
    fs = FactsStore(tmp_path / "f.db")
    cols = {r[1] for r in fs.conn.execute("PRAGMA table_info(facts)")}
    assert "ingested_at" in cols
    bad = Fact(subject="X", predicate="HAS_VIEW", object="o", canonical_id="company:X", claim="c1",
               evidence_level="C", source_kind="broker_research", sources=["d1"], valid_at="2026-84-17",
               category="view")
    fid = fs.upsert(bad)
    r = fs.conn.execute("SELECT valid_at, ingested_at FROM facts WHERE fact_id=?", (fid,)).fetchone()
    assert r["valid_at"] == "" and r["ingested_at"][:2] == "20"
    # 合并路径:垃圾日期不能赢过真日期
    good = Fact(subject="X", predicate="HAS_VIEW", object="o", canonical_id="company:X", claim="c1",
                evidence_level="C", source_kind="broker_research", sources=["d2"], valid_at="2026-05-01",
                category="view")
    fs.upsert(good)
    assert fs.conn.execute("SELECT valid_at FROM facts WHERE fact_id=?", (fid,)).fetchone()[0] == "2026-05-01"
    # 迁移幂等:再开一次不报错
    FactsStore(tmp_path / "f.db")


def test_backfill_script_core(tmp_path, monkeypatch):
    m = _load_script()
    fs = FactsStore(tmp_path / "f.db")
    facts = []
    for i, (doc, va) in enumerate([("card_a", ""), ("card_b", "2026-84-17"), ("card_c", ""), ("card_a", "2026-01-01")]):
        f = Fact(subject=f"S{i}", predicate="HAS_VIEW", object=f"o{i}", canonical_id=f"company:S{i}",
                 claim=f"claim {i}", evidence_level="C", source_kind="broker_research", sources=[doc],
                 valid_at=va, category="view")
        facts.append(fs.upsert(f))
    doc_date = {"card_a": ("2026-06-01", "zsxq_post"), "card_b": ("", ""), "card_c": ("", "")}
    items = m.facts_to_fix(fs.conn, doc_date)
    got = {fid: (old, new) for fid, old, new, _ in items}
    assert got[facts[0]] == ("", "2026-06-01")          # 补
    assert facts[1] not in got                         # 非法值已被 upsert 闸口置空,且无日期可补 → 不动
    assert facts[2] not in got and facts[3] not in got
    st = m.apply_facts(fs, items, "run1")
    assert st == {"updated": 1}
    r = fs.conn.execute("SELECT valid_at, extra FROM facts WHERE fact_id=?", (facts[0],)).fetchone()
    assert r["valid_at"] == "2026-06-01" and json.loads(r["extra"])["valid_at_source"] == "zsxq_post"
    # 幂等:再跑无事可做
    assert m.facts_to_fix(fs.conn, doc_date) == []
    # 卡片写入保 mtime
    card = tmp_path / "card_a.json"
    card.write_text(json.dumps({"id": "card_a", "date": ""}), encoding="utf-8")
    old_m = 1_600_000_000; os.utime(card, (old_m, old_m))
    monkeypatch.setattr(m, "DATA", tmp_path)
    st2 = m.apply_cards([{"path": str(card), "date": "2026-06-01", "source": "zsxq_post"},
                         {"path": str(card), "date": "", "source": ""}], "run1")
    assert st2 == {"cards_written": 1, "left_empty": 1}
    j = json.loads(card.read_text(encoding="utf-8"))
    assert j["date"] == "2026-06-01" and j["date_source"] == "zsxq_post"
    assert int(card.stat().st_mtime) == old_m
    # 回滚
    st3 = m.undo(fs, "run1")
    assert st3["facts_restored"] == 1 and st3["cards_restored"] == 1
    assert fs.conn.execute("SELECT valid_at FROM facts WHERE fact_id=?", (facts[0],)).fetchone()[0] == ""
    assert json.loads(card.read_text(encoding="utf-8"))["date"] == ""


def test_blacklist_cluster():
    m = _load_script()
    today = datetime.date(2026, 8, 27)
    # 模板日:25 个文件,独立日期全不一致 → 拉黑
    citi = [("2023-09-14", "2026-02-01")] * 10 + [("2023-09-14", "")] * 15
    # 批量打印:无独立日期,且 >1 年前 → 拉黑
    semi = [("2023-09-16", "")] * 30
    # 近期同日发布簇:无独立日期但在 1 年内 → 保留
    busy = [("2026-01-15", "")] * 41
    # 近期簇且独立日期一致 → 保留
    ok = [("2026-07-21", "2026-07-21")] * 20 + [("2026-07-21", "2026-07-19")] * 7
    # 次数不够 → 不是候选
    few = [("2023-09-15", "")] * 5 + [("", "")] * 3
    assert m.build_blacklist(citi + semi + busy + ok + few, min_count=20, today=today) == {"2023-09-14", "2023-09-16"}

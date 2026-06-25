"""语义后端选择回归测试(P0.5：bge 优先、按"已建数据"自动择优，不真调 embedding)。"""
import sqlite3

import pytest

from trading_kb import semantic as S


def _make_vec_db(path, n):
    """造一个含 n 条向量的 vectors 库(只测计数/选择逻辑，blob 随意)。"""
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE vectors(fact_id TEXT PRIMARY KEY, vec BLOB)")
    c.executemany("INSERT INTO vectors VALUES(?,?)",
                  [(str(i), b"\x00\x00\x00\x00") for i in range(n)])
    c.commit()
    c.close()


def test_auto_backend_prefers_built_index(tmp_path, monkeypatch):
    """🟠回归:ask 端自动择"已建向量最多"的后端，避免 build 进 model2vec、ask 空读 bge 的静默失效。"""
    facts_db = tmp_path / "facts.db"
    _make_vec_db(tmp_path / "vectors.db", 5)          # model2vec 有数据
    _make_vec_db(tmp_path / "vectors_bge.db", 0)      # bge 空库
    monkeypatch.setattr(S._Backend, "available", lambda self: True)   # 绕过 venv/模型文件检查
    chosen = S._auto_backend(facts_db)
    assert chosen.name == "model2vec"                 # 有数据的胜出，不空读 bge


def test_auto_backend_ties_break_to_priority(tmp_path, monkeypatch):
    """并列(数据相同)时按优先级:bge 在前。"""
    facts_db = tmp_path / "facts.db"
    _make_vec_db(tmp_path / "vectors.db", 7)
    _make_vec_db(tmp_path / "vectors_bge.db", 7)
    monkeypatch.setattr(S._Backend, "available", lambda self: True)
    assert S._auto_backend(facts_db).name == "bge"


def test_pick_backend_forces_named(monkeypatch):
    """显式 prefer 强制指定后端(build/status 用)。"""
    monkeypatch.setattr(S._Backend, "available", lambda self: True)
    assert S._pick_backend("model2vec").name == "model2vec"
    assert S._pick_backend("bge").name == "bge"
    assert S._pick_backend(None).name == "bge"        # 无 prefer → 优先级首个


def test_vec_count_missing_db_is_zero(tmp_path):
    """向量库不存在 → 计数 0(不抛)。"""
    facts_db = tmp_path / "facts.db"
    assert S._vec_count(facts_db, S._BACKENDS[0]) == 0


def test_all_unavailable_returns_none(monkeypatch, tmp_path):
    """无任何可用后端 → None(ask 优雅降级到 LIKE)。"""
    monkeypatch.setattr(S._Backend, "available", lambda self: False)
    assert S._auto_backend(tmp_path / "facts.db") is None
    assert S._pick_backend("bge") is None

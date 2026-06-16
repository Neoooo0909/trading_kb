"""全局配置:路径与运行参数。

所有路径集中此处,便于测试时重定向到临时目录。
设计依据:design_final.md §16/§17。
"""
from __future__ import annotations

import os
from pathlib import Path

# ── 项目根 ────────────────────────────────────────────────────────────────
PKG_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PKG_ROOT.parent.parent
DATA_DIR = Path(os.environ.get("TKB_DATA_DIR", PROJECT_ROOT / "data"))

# ── report_lab(证据/抽取来源,见 §6 复用)──────────────────────────────────
REPORT_LAB = Path(os.environ.get("REPORT_LAB_DIR", Path.home() / "report_lab"))
REPORT_LAB_CARDS = REPORT_LAB / "cards"
REPORT_LAB_TEXT = REPORT_LAB / "text"

# ── 存储文件 ──────────────────────────────────────────────────────────────
FACTS_DB = DATA_DIR / "facts.db"           # 时序事实层(Graphiti 等价实现)
STRUCTURE_DB = DATA_DIR / "structure.db"   # 结构关系层(LightRAG 等价实现)
ENTITY_DB = DATA_DIR / "entities.db"       # 实体注册表
SENTIMENT_DB = DATA_DIR / "sentiment.db"   # 舆情轻 lane

# ── 成色阈值(§10.4 审计)────────────────────────────────────────────────
VERIFIED_RATIO_ALERT = 0.8                 # 低于此报警人工抽查

# ── LLM 钩子开关 ──────────────────────────────────────────────────────────
# 默认关闭,分类/成色走确定性规则核心,保证模拟可离线复现(见 §15)。
# 置 TKB_USE_LLM=1 时启用 report_lab 的模型降级链。
USE_LLM = os.environ.get("TKB_USE_LLM", "0") == "1"

# ── 数据源验证开关(§8/§19)───────────────────────────────────────────────
# 默认关闭,审核走信源映射;置 TKB_USE_DATA_VERIFY=1 启用 iFinD/tdx 实查(耗额度)。
USE_DATA_VERIFY = os.environ.get("TKB_USE_DATA_VERIFY", "0") == "1"

# ── 联网权威信源开关(web_enrich)─────────────────────────────────────────
# 默认关闭(离线可复现);置 TKB_USE_WEB=1 启用,只采信权威信源(公告/投行/权威媒体)。
USE_WEB = os.environ.get("TKB_USE_WEB", "0") == "1"


def ensure_data_dir() -> None:
    """确保数据目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

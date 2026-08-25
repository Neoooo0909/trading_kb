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
# 本文件默认关闭,分类/成色走确定性规则核心,保证测试/模拟可离线复现(见 §15)。
# 注意口径:`./tkb` 启动器把默认翻转为开(export TKB_USE_LLM=1)——日常使用
# 全 LLM,离线复现只在绕过启动器直接 python -m 时成立。两处是有意分工,勿"统一"。
USE_LLM = os.environ.get("TKB_USE_LLM", "0") == "1"

# ── 数据源验证开关(§8/§19)───────────────────────────────────────────────
# 默认关闭,审核走信源映射;置 TKB_USE_DATA_VERIFY=1 启用 iFinD/tdx 实查(耗额度)。
USE_DATA_VERIFY = os.environ.get("TKB_USE_DATA_VERIFY", "0") == "1"

# ── 联网权威信源开关(web_enrich)─────────────────────────────────────────
# 默认关闭(离线可复现);置 TKB_USE_WEB=1 启用,只采信权威信源(公告/投行/权威媒体)。
USE_WEB = os.environ.get("TKB_USE_WEB", "0") == "1"

# ── 环境感知重估开关(revalue,C)─────────────────────────────────────────────
# 默认关闭(离线可复现);置 TKB_REVALUE=1 或 ask --revalue 启用,拉实时量价/估值,
# 把存量事实放进当前定价框架重估(见 revalue.py)。取数直连 tdx/ifind,失败静默降级。
USE_REVALUE = os.environ.get("TKB_REVALUE", "0") == "1"


# ── 检索(2026-08-25 召回缺口修复,见 docs/RECALL_FIX_PLAN_20260825.md)──────────
# 证券实体走"精准快路径"(只用自有事实、不跑语义、启用跨证券别名过滤)所需的最少自有
# active 事实数。注册表里 10.9 万个 company: 实体中 7.5 万零事实、2 万单事实(多为
# "AIDC用电需求""HBM先进封装"这类被登记成公司的短语),按前缀一律当证券会让查询被劫持
# 进快路径、其余候选全丢。真上市码 / 带 stock_code 的实体不受此阈值约束。
FAST_PATH_MIN_FACTS = int(os.environ.get("TKB_FAST_PATH_MIN_FACTS", "10"))
# LLM 合成层看到的材料包上限(字符)。旧值 8000 只够证据链前 ~70 条,候选池 500+ 时
# 排在后面的相关事实对 LLM 等于不存在。
SYNTH_MATERIAL_CHARS = int(os.environ.get("TKB_SYNTH_MATERIAL_CHARS", "24000"))


def ensure_data_dir() -> None:
    """确保数据目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

"""通达信公告通道的纯解析层测试（不联网）。

覆盖跨源标题匹配这个易错点：巨潮用公司全称前缀，通达信用简称前缀，
两边指同一条公告，匹配错会导致摘要贴错股票——比贴不上更糟。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# scripts/ 不入库(数据源工具,.gitignore);fresh clone 无此模块时跳过而非收集报错
tdx = pytest.importorskip("tdx_announcement")


def test_parse_title_拆出简称与代码():
    name, code, body = tdx.parse_title("联科科技（001207）：联科科技关于回购公司股份方案的公告")
    assert (name, code) == ("联科科技", "001207")
    assert body == "联科科技关于回购公司股份方案的公告"


def test_parse_title_无代码时原样返回():
    name, code, body = tdx.parse_title("A股三大指数集体收涨")
    assert name == "" and code == ""
    assert body == "A股三大指数集体收涨"


def test_core_title_剥关于前缀():
    # 回归：曾用 re.search 的 `.*公告$` 分支从位置 0 命中，前缀根本没被剥掉
    full = tdx.core_title("山东联科科技股份有限公司关于回购公司股份方案的公告")
    short = tdx.core_title("联科科技关于回购公司股份方案的公告")
    assert full == short == "关于回购公司股份方案的公告"


def test_core_title_无关于时剥公司名后缀():
    assert tdx.core_title("山东联科科技股份有限公司回购股份报告书") == "回购股份报告书"


def test_titles_match_跨源同一条():
    assert tdx.titles_match("河南神火煤电股份有限公司关于持股5%以上股东减持计划实施完毕的公告",
                            "神火股份关于持股5%以上股东减持计划实施完毕的公告")
    # 仅前缀不同、核心段互为后缀
    assert tdx.titles_match("胜科纳米股东减持股份计划公告", "股东减持股份计划公告")


def test_titles_match_不同公告不误配():
    assert not tdx.titles_match("关于回购公司股份方案的公告", "关于股东减持股份计划的公告")
    # 短串不走后缀包含，避免"的公告"这类共同尾巴造成假阳性
    assert not tdx.titles_match("的公告", "关于回购公司股份方案的公告")


def test_summary_index_剔套话与短摘要():
    rows = [
        {"code": "002670", "body": "关于收到中国证券监督管理委员会立案告知书的公告",
         "summary": "国盛证券股份有限公司于 2026 年 7 月 31 日收到中国证监会下发的《立案告知书》，"
                    "因涉嫌违反《证券经纪业务管理办法》关于账户实名制等规定，决定对公司立案。"},
        {"code": "605199", "body": "关于董事会秘书离任的公告",
         "summary": "ST葫芦娃（605199）：关于董事会秘书离任的公告。公告详情请查看附件"},
        {"code": "600000", "body": "关于某事项的公告", "summary": "太短"},
    ]
    idx = tdx.summary_index(rows)
    assert set(idx) == {"002670"}
    assert tdx.find_summary(idx, "002670", "国盛证券股份有限公司关于收到中国证券监督管理委员会立案告知书的公告")
    assert tdx.find_summary(idx, "605199", "关于董事会秘书离任的公告") == ""
    # 代码对不上绝不返回别家的摘要
    assert tdx.find_summary(idx, "000001", "关于收到中国证券监督管理委员会立案告知书的公告") == ""


def test_频道常量与页长约束():
    # Summary 只在 pageSize<=20 时下发，常量写死了这个实测约束
    assert tdx.PAGE_SIZE_WITH_SUMMARY <= 20
    assert tdx.CH_NOTICE == "307" and tdx.CH_STAR == "318"

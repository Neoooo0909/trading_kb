"""实体质量校验(治本·防垃圾实体污染注册表)。

抽取偶把"日期/纯数值/法条/地域市场/通用词"当实体登记(如 `material:2025年`、
`concept:Part 52`、`company:巴西市场`),导致论断被错挂到这些非实体上。本模块给出
**高精度、保守**的垃圾判定(宁缺毋滥,经全库 17.6 万实体校准:命中 471 个/影响 106 条事实,
逐条核对无误删 stock/fund/index 等代码背书实体)。

用于两处:① entity_registry 注册闸(拦新垃圾);② 一次性清洗脚本(删存量垃圾+解链事实)。
判定只作用于 concept/company/material/product/person;**stock/fund/index 永不判垃圾**
(代码背书或受控小表,天然干净)。
"""
from __future__ import annotations

import re

# 受代码/受控小表背书,永不判垃圾
_SAFE_TYPES = {"stock", "fund", "index"}

# 纯日期/季度/年月(实体不该是时间)
_DATE = re.compile(
    r"^(19|20)\d{2}$"                      # 1974 / 2025
    r"|^\d{4}\s*年$"                       # 2025年
    r"|^\d{4}年\d{1,2}月(\d{1,2}日)?$"     # 2026年6月 / 2026年6月10日
    r"|^[1-4]Q\d{2}$"                      # 1Q26
    r"|^\d{4}Q[1-4]$"                      # 2026Q1
    r"|^\d{4}[-/]\d{1,2}([-/]\d{1,2})?$"   # 2026-06 / 2026/06/10
)
# 法条/协议/法案(政策引用,非实体)
_LEGAL = re.compile(
    r"法案|条款|草案|停火|和平协议|Part\s*\d+|第[\d一二三四五六七八九十]+条|《[^》]*法[^》]*》")
# 地域+市场(地理区域,非公司)
_GEOMKT = re.compile(
    r"^(巴西|中东|中国|海外|欧洲|美国|印度|东南亚|国内|全球|亚太|拉美|非洲|日本|韩国|台湾|香港)市场$")
# 纯数值/币种金额(仅 concept/material 判垃圾,避开 company:1688 之类正当平台名)。
# 允许多个单位连缀,覆盖"3.5亿元/35亿元/100万元"等组合金额(单个 ? 会漏判)。
_NUM = re.compile(r"^[\d.,]+\s*(?:%|％|亿|万|元|美元|bn|mn|億|倍|pct|bps|TWD|USD|RMB|个|名){0,3}$", re.I)
# 通用词(精确匹配才算;作为独立实体名时无意义)
_GENERIC = {
    "开工", "市场", "客户", "公司", "行业", "增长", "上游", "下游", "龙头", "板块",
    "题材", "概念", "业绩", "订单", "需求", "供给", "产能", "政策", "利好", "利空",
    "估值", "成本", "价格", "收入", "利润", "毛利", "净利", "风险", "机会",
}


# 投行/券商(研报作者/评级方,几乎不是论断主体)——主体归属时从候选里剔除,
# 治"Mizuho upgrades PLTR→挂Mizuho""高盛对MiniMax给目标价→挂高盛"类误挂。
_IB_FIRMS = {
    "goldman", "morgan stanley", "hsbc", "ubs", "jpmorgan", "j.p. morgan", "jp morgan",
    "citi", "citigroup", "barclays", "nomura", "bernstein", "jefferies", "macquarie",
    "bofa", "bank of america", "credit suisse", "deutsche", "wells fargo", "rbc",
    "mizuho", "daiwa", "clsa", "wedbush", "evercore", "cowen", "piper", "raymond james",
    "stifel", "keybanc", "truist", "baird", "redburn", "新韦德", "伯恩斯坦",
    "中金", "中信证券", "中信建投", "国泰君安", "海通", "华泰", "广发", "招商证券",
    "申万", "兴业证券", "光大证券", "东方证券", "国信证券", "方正证券", "东兴证券",
    "中泰证券", "华安证券", "东北证券", "长城证券", "中邮证券", "民生证券", "国金证券",
    "天风", "安信证券", "西部证券", "浙商证券", "高盛", "摩根士丹利", "摩根大通",
}
# 免责/方法学/评级分布样板(非真实事实,不应归属到任何公司)。含投行报告"家具"文本。
_DISCLAIMER = re.compile(
    r"received compensation|owns 1%|provides ratings|disclaimer|investment banking"
    r"|making a market|beneficially own|分析师声明|利益冲突|price target.{0,8}based on"
    r"|distribution of (equity )?ratings|investment banking services|catalyst watch"
    r"|registration (no|number)|securities \([^)]*\) (ltd|limited)|本研究报告仅|不构成投资建议"
    r"|评级(分为|说明)|超配/平配|买入/(增持|持有)|分析师(评级|声明)体系"
    r"|compliance officer|合规官|rating is [OUEN][\s,.]|rated [OUEN][\s,.]"
    r"|对.{0,12}提供或寻求.{0,8}投资银行", re.I)

# 指代/匿名"主体公司"标记:论断主语是研报标的本身(公司/本公司/集团),被点名的他方是
# 对手方(收购对象/客户/供应商)。bare(前面非名字字符)才算,避免误伤 "精智达公司…"。
# **刻意不含"我们/我司"**——那是分析师口吻,此时被点名的实体反而是主体(如"我们上调中国太平目标价"
# 主体=中国太平),与"公司"语义相反。治"公司收购X→错挂X"的方向性误归属。
_ANAPHOR = re.compile(r"(?<![一-龥A-Za-z0-9·])(公司|本公司|该公司|集团)")

# 关系/角色模式:论断里被点名的实体多是**对手方**(收购对象/客户/合作方),非主体。
# 命中则不走"点名匹配"(否则方向性挂反),改由卡片主导主体(研报标的,title 锚定)归属。
# 只收**高置信、方向明确**的模式——刻意排除"中标/客户/供货"等裸词(X中标=X是主体,会误伤)。
_RELATIONSHIP = re.compile(
    r"收购|并购|被.{0,4}(收购|并购)|合资|参股|控股|入股"
    r"|与[一-龥A-Za-z·]{1,12}(战略合作|合作|合资|签[订署])"
    r"|[给向为][一-龥A-Za-z·]{1,12}(供货|供应|配套|提供|代工)"
    r"|成为[一-龥A-Za-z·]{1,12}(供应商|客户|代工|独家)"
    r"|是[一-龥A-Za-z·]{1,12}的?(供应商|客户|代工厂)"
    r"|acquire|acquisition|partnership with")


def is_ib_firm(name) -> bool:
    """是否投行/券商(研报作者/评级方)。"""
    if not isinstance(name, str):
        return False
    low = name.lower().strip()
    return any(k in low for k in _IB_FIRMS)


def is_disclaimer(text) -> bool:
    """是否免责/方法学样板文本(非真实事实)。"""
    return bool(isinstance(text, str) and _DISCLAIMER.search(text))


def resolve_claim_subject(text, card_subj_ents) -> str | None:
    """从论断文本里,按"该卡实体白名单"挑被点名的主体(高精度,治未知主体)。

    card_subj_ents: 该卡的 (name, kind) 列表,**已剔除垃圾/投行/作者券商**。
    规则:论断点名其中**恰一个** → 它;点名多个但**恰一个是股票** → 取股票;否则 None。
    比"对全注册表 17 万别名做最长匹配"精度高得多——候选只是这篇研报自己的公司,
    且排除了评级方,故 "Mizuho upgrades PLTR" 只会命中 PLTR。
    """
    if not isinstance(text, str) or not card_subj_ents:
        return None
    hit = [(n, k) for n, k in card_subj_ents if n and n in text]
    if len(hit) == 1:
        return hit[0][0]
    if len(hit) > 1:
        stocks = [n for n, k in hit if k == "stock"]
        if len(stocks) == 1:
            return stocks[0]
    return None


def has_anaphor(text) -> bool:
    """论断是否用指代/匿名主语(bare 公司/我们/本公司…),即真实主体是研报标的而非被点名的对手方。"""
    return bool(isinstance(text, str) and _ANAPHOR.search(text))


def has_relationship(text) -> bool:
    """论断是否含关系/角色动词(收购/供货/客户/合作…),即被点名实体多为对手方而非主体。"""
    return bool(isinstance(text, str) and _RELATIONSHIP.search(text))


def card_dominant_subject(card: dict, subj_ents: list, text: str) -> str | None:
    """卡片主导主体(B+/C+,治隐含主体/指代/关系):仅 type=company 个股研报,锚定 title。

    个股研报的论断默认在讲标的公司——即便用"公司"指代、或点了客户/收购对象等对手方。
    故以 **title 点名的唯一 subj 实体**(多个优先股票)为主体;title 无命中则退而用卡片唯一
    subj 实体。**不因论断点了对手方就放弃**(对手方本就该出现在关系型论断里),由 title 定主体。
    经抽检 ~95-97% 精度。
    """
    if card.get("type") != "company" or not subj_ents:
        return None
    title = card.get("title") or ""
    in_title = [(n, k) for n, k in subj_ents if n in title]
    if in_title:
        uniq = list(dict.fromkeys(n for n, _ in in_title))
        if len(uniq) == 1:
            return uniq[0]
        stocks = [n for n, k in in_title if k == "stock"]
        return stocks[0] if len(stocks) == 1 else None
    uniq = list(dict.fromkeys(n for n, _ in subj_ents))   # title 无命中 → 卡片唯一 subj
    return uniq[0] if len(uniq) == 1 else None


def attribute_subject(text, card: dict) -> str | None:
    """统一主体归属(精度优先,治本总入口)。顺序:

    ① 免责/样板 → None(本就非事实);
    ② 指代论断("公司…") → 卡片主导主体(title 锚定),不点名匹配(否则挂到对手方);
    ③ 点名匹配卡内白名单(剔投行)。但**关系型 + 只点了一个实体**时,那个实体多半是
       对手方(收购对象/客户),改用主导主体——而关系型点了**多个**(如"X与Y合作")时
       resolve 的"优先股票"已能挑对主体(X),照用,不误伤;
    ④ 仍无 → 卡片主导主体(B+/C+)。
    """
    if is_disclaimer(text):
        return None
    subj_ents = card_subject_entities(card.get("entities") or [], card.get("broker", ""))
    if has_anaphor(text):
        return card_dominant_subject(card, subj_ents, text)
    named = resolve_claim_subject(text, subj_ents)
    if named:
        hits = [n for n, _ in subj_ents if n in text]
        if has_relationship(text) and len(hits) == 1:        # 单点名+关系 → 多半是对手方
            return card_dominant_subject(card, subj_ents, text)
        return named
    return card_dominant_subject(card, subj_ents, text)


def card_subject_entities(card_entities_iter, broker: str = "") -> list:
    """从卡片实体列表筛出"可作主体的公司/股票"(剔垃圾/投行/作者券商),供 resolve_claim_subject。

    card_entities_iter: 可迭代的 dict({name,kind,...});broker: 卡片作者(同样剔除)。
    返回去重保序的 (name, kind) 列表。
    """
    bk = (broker or "").lower().strip()
    out, seen = [], set()
    for e in card_entities_iter:
        if not isinstance(e, dict):
            continue
        name, kind = e.get("name"), e.get("kind")
        if (kind in ("stock", "company") and isinstance(name, str) and len(name.strip()) >= 2
                and not is_garbage_entity(name, kind) and not is_ib_firm(name)
                and not (bk and bk in name.lower())):
            if name not in seen:
                seen.add(name)
                out.append((name, kind))
    return out


def is_garbage_entity(name, type_: str = "concept") -> bool:
    """判断 (name, type_) 是否为垃圾实体(高精度,保守)。stock/fund/index 一律 False。"""
    if not isinstance(name, str):
        return True                          # 非字符串名本就畸形,拒之
    n = name.strip()
    if not n:
        return True
    if type_ in _SAFE_TYPES:
        return False                         # 代码/受控背书,永远当真实体
    if _DATE.match(n):
        return True
    if _LEGAL.search(n):
        return True
    if _GEOMKT.match(n):
        return True
    if n in _GENERIC:
        return True
    if type_ in ("concept", "material") and _NUM.match(n):
        return True                          # 纯数值/币种(不含 company,留 1688 类)
    return False

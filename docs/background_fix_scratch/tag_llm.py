"""LLM 二次分档：从 bg_sample.jsonl 分层抽 400 条 background，DeepSeek 打标（每批 20 条）。"""
import json, random, sys, re
sys.path.insert(0, str(__import__("pathlib").Path.home()/"report_lab/scripts"))
import common
random.seed(7)
rows=[json.loads(l) for l in open("bg_sample.jsonl",encoding="utf-8")]
quota={"zsxq_posts":150,"zsxq_research":125,"ima":125}
sample=[]
for lane,n in quota.items():
    xs=[r for r in rows if r["lane"]==lane]; random.shuffle(xs); sample+=xs[:n]
PROMPT="""你是A股投研知识库的质检员。下面每条是从投研社群帖子/研报里抽出来的一条"论断"(claim+证据+实体+数字)。
它们全部被规则分流器判成了 background(定性背景)并【被直接丢弃、不入库】。请你逐条判断它真实属于哪一档，只按下面四档打标：
N = 带具体数字/日期/事件的可证伪硬事实(订单/产能/价格/财务/出货/政策/资金流/技术参数等)，应作为硬事实入库
Q = 有明确主体(具体公司/股票/板块/产品)的定性投研论断(推荐/逻辑/竞争地位/风险/展望)，无硬数字但用户查该主体时会希望检索到
R = 产业链/供应/竞争/归属等结构关系
B = 真背景：无具体主体或泛泛的宏观/情绪复述、口号套话、免责模板、无信息量、纯行情描述("大盘走低")
只输出 JSON 数组，每项 {{"i": 序号, "label": "N|Q|R|B"}}，不要解释。

条目：
{items}"""
out=open("bg_tagged.jsonl","w",encoding="utf-8")
B=20
for k in range(0,len(sample),B):
    batch=sample[k:k+B]
    items="\n".join(f"[{i}] claim: {r['claim'][:160]} | evidence: {r['evidence'][:160]} | entities: {r['entities'][:5]} | numbers: {r['numbers'][:4]}" for i,r in enumerate(batch))
    resp=common.chat(PROMPT.format(items=items), max_tokens=800, tier="extract") or ""
    m=re.search(r"\[.*\]", resp, re.S)
    labels={}
    if m:
        try:
            for o in json.loads(m.group(0)): labels[int(o["i"])]=o["label"]
        except Exception as e: print("parse fail batch",k,e, file=sys.stderr)
    for i,r in enumerate(batch):
        r["llm_label"]=labels.get(i); out.write(json.dumps(r,ensure_ascii=False)+"\n")
    out.flush(); print(f"batch {k//B} done, labeled {len(labels)}", file=sys.stderr)
out.close()

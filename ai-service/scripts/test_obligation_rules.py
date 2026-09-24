# ═══════════════════════════════════════════════════════════════════
# 规则通道义务方向信号单测（v4：OBLIGATION_*_PATTERNS + 摘录函数）
#   正例：E1/E3/E7/E8 文本方向判定
#   反例（防误配）：违约金 / 租金 / 服务费 不触发方向分
#   摘录：义务条款在 3000 字窗口外也能抽出
# 不调 LLM，纯规则，秒级完成。
# ═══════════════════════════════════════════════════════════════════
import sys
import logging

sys.path.insert(0, "/app")
logging.basicConfig(level=logging.CRITICAL)

from app.services.classifier import ContractClassifier

clf = ContractClassifier(llm_client=object(), prompt_manager=None)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    tag = "✅" if cond else "❌"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {tag} {name}  {detail}")


print("═" * 90)
print("一、义务方向正则（防误配是重点）")
print("═" * 90)

# 正例：应命中对应方向
buy_cases = [
    ("甲方支付预付款", "甲方应于合同签订后10日内向乙方支付预付款30%"),
    ("甲方支付价款", "甲方向乙方支付价款共计582,000元"),
    ("甲方付款剩余", "货到验收合格后30日内甲方付款剩余70%"),
    ("乙方供货", "乙方向甲方提供办公设备一批"),
    ("乙方交付设备", "乙方负责交付设备并安装"),
]
for name, t in buy_cases:
    hit = any(p.search(t) for p in ContractClassifier.OBLIGATION_BUY_PATTERNS)
    miss = any(p.search(t) for p in ContractClassifier.OBLIGATION_SELL_PATTERNS)
    check(f"BUY 命中: {name}", hit and not miss)

sell_cases = [
    ("乙方支付价款", "乙方向甲方支付价款共计100,000元"),
    ("甲方供货", "甲方向乙方供应服务器30台"),
    ("甲方交付货物", "甲方负责交付货物至乙方指定仓库"),
]
for name, t in sell_cases:
    hit = any(p.search(t) for p in ContractClassifier.OBLIGATION_SELL_PATTERNS)
    miss = any(p.search(t) for p in ContractClassifier.OBLIGATION_BUY_PATTERNS)
    check(f"SELL 命中: {name}", hit and not miss)

# 反例：不得误配
neg_cases = [
    ("违约金-乙方付", "乙方向甲方支付违约金50,000元"),
    ("违约金-甲方付", "甲方逾期付款的，应向乙方支付违约金"),
    ("赔偿金", "甲方向乙方支付赔偿金200,000元"),
    ("租金-甲方付", "甲方应于每月5日前支付租金45,000元"),
    ("服务费", "甲方按季度支付服务费用"),
    ("仅出现'分期'字样", "双方约定分期履行"),
]
for name, t in neg_cases:
    hit_b = any(p.search(t) for p in ContractClassifier.OBLIGATION_BUY_PATTERNS)
    hit_s = any(p.search(t) for p in ContractClassifier.OBLIGATION_SELL_PATTERNS)
    check(f"不误配: {name}", not hit_b and not hit_s, f"(buy={hit_b}, sell={hit_s})")

print()
print("═" * 90)
print("二、_rule_classify 集成（方向分是否把类型判对）")
print("═" * 90)

E7 = ("设备买卖合同",
      "设备买卖合同\n\n甲方：北京华信科技有限公司\n乙方：上海联创贸易有限公司\n"
      "一、乙方向甲方提供办公设备一批，于合同生效后20日内送达甲方指定地点。\n"
      "二、合同总金额582,000元，甲方于合同签订后10日内向乙方支付预付款30%，"
      "货到验收合格后30日内支付剩余70%。\n"
      "三、乙方逾期交货的，每逾期一日按合同总额的0.5%支付违约金。")
E8 = ("战略合作框架协议",
      "战略合作框架协议\n\n甲方：北京华信科技有限公司\n乙方：上海联创贸易有限公司\n"
      "一、乙方向甲方供应服务器30台，单价19,400元，总金额582,000元。\n"
      "二、甲方应于合同签订后10日内支付预付款30%，货到验收合格后30日内支付剩余70%。\n"
      "三、乙方逾期交货的，每逾期一日按合同总额的0.5%支付违约金。")
E1 = ("产品购销合同",
      "产品购销合同\n\n供方（甲方）：上海联创贸易有限公司\n需方（乙方）：北京华信科技有限公司\n"
      "经甲乙双方协商，甲方向乙方供应办公设备一批，总金额582,000元。")
E6 = ("技术服务协议",
      "技术服务协议\n\n甲方（委托方）：某某科技有限公司\n乙方（受托方）：某某信息技术有限公司\n"
      "受托方为委托方提供运维服务，服务费每月45000元，按季度支付。")
RENT = ("房屋租赁合同",
        "房屋租赁合同\n\n出租方（甲方）：某某置业公司\n承租方（乙方）：某某商贸公司\n"
        "月租金45,000元，押一付三，租赁期一年。")

t, c = clf._rule_classify(E7[1], E7[0])
check("E7 无角色标注 → 采购", t == "采购合同", f"({t}, {c})")
t, c = clf._rule_classify(E8[1], E8[0])
check("E8 标题误导 → 采购（方向分补位）", t == "采购合同", f"({t}, {c})")
t, c = clf._rule_classify(E1[1], E1[0])
check("E1 视角反转 → 销售", t == "销售合同", f"({t}, {c})")
t, c = clf._rule_classify(E6[1], E6[0])
check("E6 服务合同 → 服务（不被方向分污染）", t == "服务合同", f"({t}, {c})")
t, c = clf._rule_classify(RENT[1], RENT[0])
check("租赁合同 → 租赁（不被方向分污染）", t == "租赁合同", f"({t}, {c})")

print()
print("═" * 90)
print("三、义务条款摘录（3000 字窗口外的条款必须能抽出）")
print("═" * 90)

# 构造长文本：filler 不含触发词、按行拆分；义务条款插在约第 160 行（>3000 字窗口外）
filler_line = "本条为背景描述，仅陈述双方基本情况，与钱货义务无关。"
filler = "\n".join([filler_line] * 150)
long_text = ("合同正文开头。\n" + filler
             + "\n第九条 甲方应于验收合格后30日内向乙方支付剩余款项582,000元。\n"
             + "\n".join([filler_line] * 20))
excerpt = clf._extract_obligation_excerpt(long_text)
check("摘录包含窗口外条款", "支付剩余款项" in excerpt, f"(长度={len(excerpt)})")
check("摘录行数受限", len(excerpt.splitlines()) <= 12)

empty = clf._extract_obligation_excerpt("本条仅陈述双方基本情况与一般性约定，与钱货义务无关。" * 5)
check("无命中 → 占位符", empty == "（未检出明显义务条款）")

print()
print("═" * 90)
print(f"结果: {PASS} 通过 / {FAIL} 失败")
print("═" * 90)
sys.exit(1 if FAIL else 0)

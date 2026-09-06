# ════════════════════════════════════════════════
# payment_schedule.py — 付款条款 → 收付款计划节点解析（B5 业财一体化）
# 规则优先：中文合同付款条款模式性强（比例/金额/条件/期限），
# 规则解析稳定且零成本；解析不出任何节点时返回空列表（宁缺勿错）。
# ════════════════════════════════════════════════
import logging
import re

_logger = logging.getLogger(__name__)

# ── 节点类型关键词 → milestone 枚举（与 contract.payment.plan 对齐） ──
MILESTONE_RULES = [
    (re.compile(r"预付|定金|订金|首付款"), "prepayment"),
    (re.compile(r"到货|交货|发货|交付"), "on_delivery"),
    (re.compile(r"验收|完工|竣工"), "on_completion"),
    (re.compile(r"质保|保证金|尾款|余款|结算款"), "final"),
    (re.compile(r"分期|月结|季度|按月|按季"), "installment"),
]

# ── 百分比：30% / 30 % / 百分之三十 ──
RATIO_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
CN_RATIO_PATTERN = re.compile(r"百分之([一二三四五六七八九十]+)")

# ── 金额：支付 174000 元 / 即 348,000 元 / 58000 元 ──
AMOUNT_PATTERN = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:万)?元")

# ── 期限：5 个工作日内 / 30 日内 / 10天内 ──
DAYS_PATTERN = re.compile(r"(\d+)\s*(?:个)?\s*(?:工作)?日内")

# ── 租金类条款：月租金 45000 元 / 年租金 54 万元 ──
RENT_MONTHLY_PATTERN = re.compile(r"月租金\s*([\d,]+(?:\.\d+)?)\s*(万)?\s*元")
RENT_YEARLY_PATTERN = re.compile(r"年租金\s*([\d,]+(?:\.\d+)?)\s*(万)?\s*元")
# 大写金额附小写形式：「月租金为人民币肆万伍仟元整（￥45,000.00）」
# 非贪婪且禁止跨越句读（。；;，,），避免从"月租金"吞到无关句子的数字
RENT_MONTHLY_PAREN_PATTERN = re.compile(r"月租金[^。；;，,]*?￥\s*([\d,]+(?:\.\d+)?)")
RENT_YEARLY_PAREN_PATTERN = re.compile(r"年租金[^。；;，,]*?￥\s*([\d,]+(?:\.\d+)?)")

_MILESTONE_LABEL = {
    "prepayment": "预付款", "on_delivery": "到货验收",
    "on_completion": "完工验收", "installment": "分期支付",
    "final": "尾款", "other": "其他",
}


def _cn_ratio_to_float(cn: str) -> float | None:
    """百分之三十 → 30.0（支持 十/二十/二十五 等简单组合）"""
    cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if cn in ("十",):
        return 10.0
    if "十" in cn:
        parts = cn.split("十")
        tens = cn_map.get(parts[0], 1) if parts[0] else 1
        units = cn_map.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return float(tens * 10 + units)
    return float(cn_map.get(cn, 0)) if cn in cn_map else None


def _expand_rent_nodes(seg: str) -> list[dict] | None:
    """租金类条款展开：月租金/年租金 + 支付周期 → 多期租金节点。

    例："月租金45000元，年租金540000元，按季度支付，押一付三"
      → 4 期 × 135000（总额与年租金自洽）。
    未出现"月租金/年租金"字样时返回 None，交由通用规则解析。
    """
    m_month = RENT_MONTHLY_PATTERN.search(seg)
    m_year = RENT_YEARLY_PATTERN.search(seg)
    if not m_month and not m_year:
        # 直接数字形式未命中 → 尝试「大写整（￥小写）」形式
        m_month = RENT_MONTHLY_PAREN_PATTERN.search(seg)
        m_year = RENT_YEARLY_PAREN_PATTERN.search(seg)
        if not m_month and not m_year:
            return None

    def _amt(m):
        v = float(m.group(1).replace(",", ""))
        # 括号小写形式只有 1 个捕获组（无"万"单位组）
        return v * 10000.0 if len(m.groups()) > 1 and m.group(2) else v

    monthly = _amt(m_month) if m_month else None
    yearly = _amt(m_year) if m_year else (monthly * 12.0 if monthly else None)
    if not yearly:
        return None

    # 支付周期 → 每期金额与期数（总额 = 年租金，与合同金额提取口径一致）
    if re.search(r"押一付三|按季度|季度支付|按季支付|按季", seg):
        per_period = (monthly or 0.0) * 3.0 or yearly / 4.0
        n = max(1, round(yearly / per_period))
        label = "季度租金"
    elif re.search(r"按半年|半年付", seg):
        per_period = yearly / 2.0
        n = 2
        label = "半年租金"
    elif re.search(r"按月支付|按月付|月付", seg):
        per_period = monthly or yearly / 12.0
        n = max(1, round(yearly / per_period))
        label = "月租金"
    else:
        per_period = yearly
        n = 1
        label = "年租金"

    days = None
    m = DAYS_PATTERN.search(seg)
    if m:
        days = int(m.group(1))

    nodes = []
    for i in range(n):
        nodes.append({
            "name": "%s（第%d期）" % (label, i + 1) if n > 1 else label,
            "milestone": "installment",
            "ratio": None,
            "amount": round(per_period, 2),
            "days_after_sign": days,
            "description": seg[:500],
        })
    _logger.info("💰 租金条款展开：%d 期 × %.0f 元（总额 %.0f）", n, per_period, per_period * n)
    return nodes


def parse_payment_terms(payment_terms: str) -> list[dict]:
    """付款条款文本 → 收付款计划节点列表

    返回元素:
      name        节点名称（第 N 期·预付款）
      milestone   prepayment/on_delivery/on_completion/installment/final/other
      ratio       比例（0-1，如 0.30），可为 None
      amount      固定金额（元），可为 None
      description 条款原文摘要
    解析不出任何节点时返回 []。
    """
    if not payment_terms or not payment_terms.strip():
        return []

    # 全文级租金展开：LLM 可能把租金金额与支付周期拆进不同分句，
    # 因此先用全文做一次"月租/年租 + 周期"匹配
    nodes = []
    rent_nodes = _expand_rent_nodes(payment_terms)
    rent_matched = rent_nodes is not None
    if rent_matched:
        nodes.extend(rent_nodes)

    # 按分句切分（；;。换行），保留有效片段
    segments = [s.strip() for s in re.split(r"[；;。\n]", payment_terms) if s.strip()]
    for seg in segments:
        # 已由全文租金展开覆盖的句子不再重复解析
        if rent_matched and (
            RENT_MONTHLY_PATTERN.search(seg) or RENT_YEARLY_PATTERN.search(seg)
            or RENT_MONTHLY_PAREN_PATTERN.search(seg) or RENT_YEARLY_PAREN_PATTERN.search(seg)
        ):
            continue
        # 押金为可退保证金，不计入付款计划（避免计划总额虚高）
        if re.search(r"押金", seg) and not re.search(r"预付|定金|订金", seg):
            continue

        ratio = None
        amount = None

        m = RATIO_PATTERN.search(seg)
        if m:
            ratio = float(m.group(1)) / 100.0
        else:
            m = CN_RATIO_PATTERN.search(seg)
            if m:
                v = _cn_ratio_to_float(m.group(1))
                if v is not None:
                    ratio = v / 100.0

        m = AMOUNT_PATTERN.search(seg)
        if m:
            amount = float(m.group(1).replace(",", ""))
            # "万元" 变体
            if re.search(r"{}\s*万元".format(re.escape(m.group(1))), seg):
                amount *= 10000.0

        # 该句必须含比例或金额才算一个节点（纯条件描述不算）
        if ratio is None and amount is None:
            continue

        milestone = "other"
        for pattern, ms in MILESTONE_RULES:
            if pattern.search(seg):
                milestone = ms
                break

        days = None
        m = DAYS_PATTERN.search(seg)
        if m:
            days = int(m.group(1))

        label = _MILESTONE_LABEL[milestone]
        nodes.append({
            "name": "%s（%s）" % (label, "%d%%" % round(ratio * 100) if ratio else ""),
            "milestone": milestone,
            "ratio": ratio,
            "amount": amount,
            "days_after_sign": days,
            "description": seg[:500],
        })

    # 节点名修正：去掉空括号
    for node in nodes:
        node["name"] = node["name"].replace("（）", "").strip()

    _logger.info("💰 付款条款解析：%d 个节点（条款 %d 字）", len(nodes), len(payment_terms))
    return nodes

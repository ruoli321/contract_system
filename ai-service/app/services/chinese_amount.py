# ════════════════════════════════════════════════
# chinese_amount.py — 中文大写金额 → 数字（B1 交叉校验）
# 合同惯例：大写金额具有法律优先级。提取出 amount_uppercase 后
# 换算成数字与 amount 交叉校验，不一致时在响应中标记。
# 支持：壹佰贰拾捌万元整 / 伍拾万零伍佰元 / 壹亿贰仟万元 / X元Y角Z分
# ════════════════════════════════════════════════
import re

DIGITS = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4,
          "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}
UNITS = {"拾": 10, "佰": 100, "仟": 1000}
SECTION = {"万": 1e4, "亿": 1e8}

_UPPER_CHARS = set(DIGITS) | set(UNITS) | set(SECTION)


def chinese_uppercase_to_amount(text) -> float | None:
    """中文大写金额 → 数字（元）。无法解析返回 None。

    示例:
      人民币壹佰贰拾捌万元整 → 1280000.0
      伍拾万零伍佰元       → 500500.0
      壹佰元贰角叁分       → 100.23
    """
    if not text:
        return None
    t = str(text)
    # 去前缀/别名
    t = t.replace("人民币", "").replace("￥", "").replace("圆", "元")
    t = t.replace("整", "").replace("正", "")

    # 不含大写数字 → 非大写金额文本
    if not any(ch in _UPPER_CHARS for ch in t):
        return None

    # 角分先提取再剔除（避免角位数字混入主部分，如"壹佰元贰角叁分"）
    jiao = fen = 0.0
    m = re.search(r"([壹贰叁肆伍陆柒捌玖])角", t)
    if m:
        jiao = DIGITS[m.group(1)] * 0.1
    m = re.search(r"([壹贰叁肆伍陆柒捌玖])分", t)
    if m:
        fen = DIGITS[m.group(1)] * 0.01
    main_part = re.sub(r"[壹贰叁肆伍陆柒捌玖][角分]", "", t)

    total, section, num = 0.0, 0.0, 0
    for ch in main_part:
        if ch in DIGITS:
            num = DIGITS[ch]
        elif ch in UNITS:
            section += (num or 1) * UNITS[ch]
            num = 0
        elif ch in SECTION:
            total += (section + num) * SECTION[ch]
            section = num = 0
        elif ch == "元":
            total += section + num
            section = num = 0
        # 其它字符（零、顿号等）跳过
    total += section + num

    return round(total + jiao + fen, 2) if total > 0 else None

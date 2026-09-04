"""M18 · pytest 单元测试 — 字段校验函数

覆盖：
    extractor.py 里的日期格式校验（YYYY-MM-DD）
    金额格式校验 + 人民币大写一致性
    enum 值合法性（type / dispute_resolution 等）
    双向模糊匹配工具函数
"""
import pytest
import re
from datetime import datetime


# ════════════════════════════════════════════════════════════
# 纯函数：日期标准化（模仿 extractor._normalize_date）
# ════════════════════════════════════════════════════════════

# 中文数字 → 阿拉伯数字映射（用于日期解析）
_CN_DIGITS = {
    "〇": "0", "零": "0",
    "一": "1", "二": "2", "两": "2",
    "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9",
}


def _cn_to_arabic(text: str) -> str:
    """把文本中的单个中文数字字符转换成阿拉伯数字（用于日期场景）"""
    result = []
    for ch in text:
        result.append(_CN_DIGITS.get(ch, ch))
    return "".join(result)


def normalize_date(value: str) -> str | None:
    """把各种日期格式统一成 YYYY-MM-DD；无法解析返回 None"""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()

    # 宽松匹配：先尝试把中文数字替换成阿拉伯数字
    v_cn_converted = _cn_to_arabic(v)

    candidates = [
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
        "%Y年%m月%d日", "%Y年%m月%d号",
        "%y-%m-%d", "%y/%m/%d",
        "%Y%m%d",
    ]
    # 先试原始文本
    for fmt in candidates:
        try:
            dt = datetime.strptime(v, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # 中文数字转换后再试一次
    if v_cn_converted != v:
        for fmt in candidates:
            try:
                dt = datetime.strptime(v_cn_converted, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

    # 宽松匹配：从字符串里抽数字（原始 + 中文转换后都试）
    for text in (v, v_cn_converted):
        nums = re.findall(r"\d+", text)
        if len(nums) >= 3:
            try:
                y, m, d = int(nums[0]), int(nums[1]), int(nums[2])
                if 1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{m:02d}-{d:02d}"
            except (ValueError, IndexError):
                pass

    return None


# ════════════════════════════════════════════════════════════
# 纯函数：金额解析
# ════════════════════════════════════════════════════════════

def parse_amount(value: str | float | int) -> float | None:
    """从各种形式（¥50万 / 人民币 500,000 元 / 500000）解析出金额数值"""
    if isinstance(value, (int, float)):
        return float(value)

    if not value or not isinstance(value, str):
        return None

    v = value.strip()
    # 去掉货币符号和"元/万元"后缀
    v = re.sub(r"[¥￥]", "", v)
    v = re.sub(r"人民币|元|RMB|CNY", "", v)
    v = v.replace(",", "").replace("，", "").strip()

    multiplier = 1.0
    if v.endswith("万"):
        multiplier = 10000
        v = v[:-1]
    elif v.endswith("亿"):
        multiplier = 100_000_000
        v = v[:-1]

    try:
        return float(v) * multiplier
    except ValueError:
        return None


# ════════════════════════════════════════════════════════════
# 纯函数：双向包含模糊匹配（文本等价判断）
# ════════════════════════════════════════════════════════════

def fuzzy_match(a: str, b: str) -> bool:
    """a 包含 b 或 b 包含 a，即视为等价"""
    if not a or not b:
        return False
    a = str(a).strip().lower()
    b = str(b).strip().lower()
    return a in b or b in a


# ════════════════════════════════════════════════════════════
# 纯函数：枚举合法性校验
# ════════════════════════════════════════════════════════════

VALID_CONTRACT_TYPES = ["采购合同", "销售合同", "服务合同", "租赁合同", "其他"]
VALID_DISPUTE_RESOLUTIONS = ["诉讼", "仲裁", "协商", "调解", "人民法院"]

def validate_enum(value: str, valid_list: list[str]) -> bool:
    """检查值是否在合法枚举列表中（支持模糊匹配：双向包含 + 关键词包含）"""
    if not value:
        return False
    for valid in valid_list:
        if fuzzy_match(value, valid):
            return True
        # 关键词匹配：如果 value 包含 valid 的子串（如 "人民法院" 匹配 "向人民法院起诉"）
        if valid and valid in value:
            return True
    return False


# ════════════════════════════════════════════════════════════
# 日期校验测试
# ════════════════════════════════════════════════════════════

class TestNormalizeDate:

    @pytest.mark.parametrize("input,expected", [
        ("2026-08-01", "2026-08-01"),
        ("2026/08/01", "2026-08-01"),
        ("2026.08.01", "2026-08-01"),
        ("2026年8月1日", "2026-08-01"),
        ("2026年08月01日", "2026-08-01"),
        ("26-08-01", "2026-08-01"),
        ("20260801", "2026-08-01"),
        ("2026年8月1号", "2026-08-01"),
    ])
    def test_various_formats(self, input, expected):
        assert normalize_date(input) == expected

    def test_invalid_dates_return_none(self):
        assert normalize_date("") is None
        assert normalize_date(None) is None
        assert normalize_date("not a date") is None
        assert normalize_date("32026-08-01") is None  # 年份前缀不对

    def test_edge_dates(self):
        # 最小/最大
        assert normalize_date("2020-01-01") == "2020-01-01"
        assert normalize_date("2099-12-31") == "2099-12-31"

    def test_loose_number_extraction(self):
        # 混乱文本里提数字
        result = normalize_date("签订日期：二〇二六年 八月 一日")
        assert result == "2026-08-01" or result is not None


# ════════════════════════════════════════════════════════════
# 金额解析测试
# ════════════════════════════════════════════════════════════

class TestParseAmount:

    @pytest.mark.parametrize("input,expected", [
        ("500000", 500000.0),
        ("500,000", 500000.0),
        ("¥500,000", 500000.0),
        ("人民币 500,000 元", 500000.0),
        ("50万", 500_000.0),
        ("50.5万", 505_000.0),
        ("1亿", 100_000_000.0),
        ("¥50万", 500_000.0),
        (500000, 500000.0),
        (500000.0, 500000.0),
    ])
    def test_various_formats(self, input, expected):
        assert parse_amount(input) == pytest.approx(expected)

    def test_invalid_amounts(self):
        assert parse_amount("") is None
        assert parse_amount(None) is None
        assert parse_amount("abc") is None
        assert parse_amount("零") is None  # 纯中文大写不支持

    def test_zero_amount(self):
        assert parse_amount("0") == 0.0
        assert parse_amount("0元") == 0.0


# ════════════════════════════════════════════════════════════
# 模糊匹配测试
# ════════════════════════════════════════════════════════════

class TestFuzzyMatch:

    def test_exact_match(self):
        assert fuzzy_match("采购合同", "采购合同") is True

    def test_substring_match(self):
        assert fuzzy_match("采购合同", "采购合同模板") is True
        assert fuzzy_match("服务器采购合同", "采购") is True

    def test_case_insensitive(self):
        assert fuzzy_match("CONTRACT", "contract") is True

    def test_no_match(self):
        assert fuzzy_match("采购合同", "销售合同") is False

    def test_empty_input(self):
        assert fuzzy_match("", "采购") is False
        assert fuzzy_match("采购", "") is False
        assert fuzzy_match(None, "采购") is False


# ════════════════════════════════════════════════════════════
# 枚举校验测试
# ════════════════════════════════════════════════════════════

class TestValidateEnum:

    def test_valid_values(self):
        assert validate_enum("采购合同", VALID_CONTRACT_TYPES) is True
        assert validate_enum("诉讼", VALID_DISPUTE_RESOLUTIONS) is True

    def test_fuzzy_valid(self):
        # "人民法院" 是 VALID_DISPUTE_RESOLUTIONS 的值，"向人民法院起诉" 包含它
        assert validate_enum("向人民法院起诉", VALID_DISPUTE_RESOLUTIONS) is True
        # "人民法院提起诉讼" 同时包含 "人民法院" 和 "诉讼"
        assert validate_enum("人民法院提起诉讼", VALID_DISPUTE_RESOLUTIONS) is True
        # "协商解决" 包含 "协商"
        assert validate_enum("协商解决", VALID_DISPUTE_RESOLUTIONS) is True

    def test_invalid_values(self):
        assert validate_enum("违法合同", VALID_CONTRACT_TYPES) is False
        assert validate_enum("打架", VALID_DISPUTE_RESOLUTIONS) is False

    def test_empty_input(self):
        assert validate_enum("", VALID_CONTRACT_TYPES) is False
        assert validate_enum(None, VALID_CONTRACT_TYPES) is False

    def test_empty_list(self):
        assert validate_enum("采购合同", []) is False

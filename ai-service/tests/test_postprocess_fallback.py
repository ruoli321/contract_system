# ═══════════════════════════════════════════════════════════════════
# M20 · 正则兜底测试
#
# 用 conftest 的 SAMPLE_CONTRACT_TEXT（真实合同版式）验证：
#   1. 补齐缺失字段（编号/金额/日期/甲乙方/争议解决/币种）
#   2. 不覆盖 LLM 已提取的值（fill 铁律）
#   3. 日期多格式归一化（阿拉伯/中文数字/空格变体/非法日期拒绝）
#   4. 金额锚定优先级（总金额 50 万 ≠ 单价 5 万）
# ═══════════════════════════════════════════════════════════════════
import pytest

from app.services.postprocess_fallback import fill, normalize_date
from tests.conftest import SAMPLE_CONTRACT_TEXT


# ═══════════════════════════════════════════════════════════════════
# normalize_date 单元测试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("snippet,expected", [
    ("签订日期：2026-08-01", "2026-08-01"),
    ("2026/8/1 签订", "2026-08-01"),
    ("2026 年 8 月 1 日", "2026-08-01"),          # 空格变体
    ("二〇二六年八月一日", "2026-08-01"),          # 中文数字日期
    ("二零二六年十二月三十一日", "2026-12-31"),
    ("2026-13-40 签订", None),                     # 非法日期拒绝
    ("没有日期的文本", None),
    ("", None),
])
def test_normalize_date(snippet, expected):
    assert normalize_date(snippet) == expected


# ═══════════════════════════════════════════════════════════════════
# fill 综合测试（用 conftest 真实合同样例）
# ═══════════════════════════════════════════════════════════════════

def test_fill_empty_fields_from_sample_contract():
    """LLM 全空 → 正则从样例合同补齐结构化字段"""
    empty = {
        "contract_name": None, "contract_code": None,
        "partner_a": None, "partner_b": None,
        "amount": None, "amount_uppercase": None, "currency": None,
        "sign_date": None, "effective_date": None, "expire_date": None,
        "contract_type": "采购合同", "confidence": 0.1,
        "payment_terms": None, "breach_clause": None, "dispute_resolution": None,
    }
    result = fill(empty, SAMPLE_CONTRACT_TEXT)

    assert result["contract_code"] == "CG-20260801-0001"
    # 金额锚定"合同总金额人民币 50 万元"，不能抓成单价 5 万元
    assert result["amount"] == 500000.0
    # 签订日期锚定"日期：2026 年 8 月 1 日"（落款处）
    assert result["sign_date"] == "2026-08-01"
    assert result["partner_a"] == "XX 科技有限公司"
    assert result["partner_b"] == "YY 信息技术有限公司"
    # 争议解决输出 field_dict 合法枚举值
    assert result["dispute_resolution"] == "向人民法院提起诉讼"
    assert result["currency"] == "CNY"
    # 样例合同无生效/失效日期 → 保持 None 不乱填
    assert result["effective_date"] is None
    assert result["expire_date"] is None
    # 不动的字段原样保留
    assert result["contract_type"] == "采购合同"
    assert result["contract_name"] is None


def test_fill_never_overwrites_existing_values():
    """LLM 已提取的值绝不能被正则覆盖"""
    llm_result = {
        "contract_code": "LLM-CODE-001",
        "partner_a": "LLM 提取的甲方公司",
        "amount": 999.0,
        "sign_date": "2025-01-01",
        "dispute_resolution": "提交仲裁委员会仲裁",
        "currency": "USD",
    }
    result = fill(llm_result, SAMPLE_CONTRACT_TEXT)

    assert result["contract_code"] == "LLM-CODE-001"
    assert result["partner_a"] == "LLM 提取的甲方公司"
    assert result["amount"] == 999.0
    assert result["sign_date"] == "2025-01-01"
    assert result["dispute_resolution"] == "提交仲裁委员会仲裁"
    assert result["currency"] == "USD"


def test_fill_partial_llm_result():
    """LLM 提了一半 → 只补缺失的"""
    partial = {
        "contract_code": "HT-2026-888",
        "partner_a": "甲公司",
        "partner_b": None,
        "amount": None,
    }
    result = fill(partial, SAMPLE_CONTRACT_TEXT)
    assert result["contract_code"] == "HT-2026-888"   # 已有 → 不动
    assert result["partner_a"] == "甲公司"             # 已有 → 不动
    assert result["partner_b"] == "YY 信息技术有限公司"  # 缺失 → 补齐
    assert result["amount"] == 500000.0                # 缺失 → 补齐


def test_fill_input_not_mutated():
    """fill 返回新 dict，不修改入参"""
    original = {"contract_code": None, "amount": None}
    frozen = dict(original)
    fill(original, SAMPLE_CONTRACT_TEXT)
    assert original == frozen


# ═══════════════════════════════════════════════════════════════════
# 金额锚定优先级（防"单价误当总额"回归）
# ═══════════════════════════════════════════════════════════════════

def test_amount_prefers_total_over_unit_price():
    text = "每台单价人民币 5 万元，合同总金额人民币 50 万元整。"
    result = fill({"amount": None}, text)
    assert result["amount"] == 500000.0


def test_amount_plain_yuan_with_total_label():
    text = "合同总价：500,000 元"
    result = fill({"amount": None}, text)
    assert result["amount"] == 500000.0


def test_amount_fallback_generic_rmb():
    text = "本协议涉及人民币 80 万元服务费。"
    result = fill({"amount": None}, text)
    assert result["amount"] == 800000.0

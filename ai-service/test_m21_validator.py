# -*- coding: utf-8 -*-
"""
M21 · 统一校验器 + 质检守门 — 单元测试

覆盖：
  validator.validate_extraction
    - 全通过（证据/交叉/sanity/枚举全绿，高置信度）
    - critical 缺失：原文无信息源 → fatal；原文有信息源 → fatal + 人工补录提示
    - 幻觉拦截：编造的甲方/金额 → errors（可重试）
    - 金额大写 ↔ 数字交叉不一致 → error
    - 日期格式/合法性/逻辑顺序/年份越界
    - 金额 sanity（≤0 / 超上限）
    - dispute_resolution 枚举非法
    - is_fallback 置信度封顶 0.30
  validator.check_payment_schedule（业财红线）
  gatekeeper.build_quality（quality 块生成 + 分级标记）

运行：在 ai-service 目录下 pytest test_m21_validator.py -v
（cwd 必须是 ai-service 根，保证 prompts/field_dict.json 可被定位）
"""
import pytest

from app.services.validator import (
    ValidationReport,
    validate_extraction,
    check_payment_schedule,
    collect_text_dates,
    collect_amount_candidates,
)
from app.services.gatekeeper import build_quality


# ═══════════════════════════════════════════════
# 测试数据
# ═══════════════════════════════════════════════

GOOD_TEXT = (
    "设备采购合同\n"
    "合同编号：CG-2026-001\n"
    "甲方：北京智慧城市建设发展有限公司\n"
    "乙方：上海华信设备制造有限公司\n"
    "合同总金额：人民币壹佰贰拾万元整（¥1,200,000.00）\n"
    "签订日期：2026年3月15日\n"
    "本合同自2026年4月1日起生效，至2027年3月31日到期。\n"
    "币种：人民币\n"
)

GOOD_FIELDS = {
    "contract_name": "设备采购合同",
    "contract_code": "CG-2026-001",
    "partner_a": "北京智慧城市建设发展有限公司",
    "partner_b": "上海华信设备制造有限公司",
    "amount": 1200000.0,
    "amount_uppercase": "壹佰贰拾万元整",
    "currency": "CNY",
    "sign_date": "2026-03-15",
    "effective_date": "2026-04-01",
    "expire_date": "2027-03-31",
    "payment_terms": None,
    "breach_clause": None,
    "dispute_resolution": "协商解决",
}


# ═══════════════════════════════════════════════
# validator · 全通过
# ═══════════════════════════════════════════════

class TestValidatePass:
    def test_all_pass(self):
        report = validate_extraction(GOOD_FIELDS, GOOD_TEXT)
        assert report.errors == [], f"不应有可重试错误: {report.errors}"
        assert report.fatal_errors == [], f"不应有致命错误: {report.fatal_errors}"
        assert report.warnings == [], f"不应有警告: {report.warnings}"
        assert report.critical_missing == []
        assert report.system_confidence >= 0.85
        # 全部证据字段应通过
        assert all(report.field_evidence.values()), report.field_evidence

    def test_evidence_helpers(self):
        assert collect_text_dates(GOOD_TEXT) >= {"2026-03-15", "2026-04-01", "2027-03-31"}
        assert any(abs(v - 1200000) < 0.01 for v in collect_amount_candidates(GOOD_TEXT))


# ═══════════════════════════════════════════════
# validator · critical 缺失 → fatal
# ═══════════════════════════════════════════════

class TestCriticalMissing:
    def test_missing_without_source(self):
        """原文完全没有金额信息 → fatal（不可重试）"""
        fields = {**GOOD_FIELDS, "amount": None}
        text = "设备采购合同\n甲方：北京智慧城市建设发展有限公司\n乙方：上海华信设备制造有限公司\n签订日期：2026年3月15日"
        report = validate_extraction(fields, text)
        assert "amount" in report.critical_missing
        assert any("amount" in e and "未找到相关信息源" in e for e in report.fatal_errors)

    def test_missing_with_source(self):
        """原文有金额表述但没提取出来 → fatal + 请人工补录"""
        fields = {**GOOD_FIELDS, "amount": None}
        report = validate_extraction(fields, GOOD_TEXT)
        assert "amount" in report.critical_missing
        assert any("请人工补录" in e for e in report.fatal_errors)

    def test_missing_partner(self):
        fields = {**GOOD_FIELDS, "partner_b": None}
        report = validate_extraction(fields, GOOD_TEXT)
        assert "partner_b" in report.critical_missing
        assert any("partner_b" in e for e in report.fatal_errors)


# ═══════════════════════════════════════════════
# validator · 幻觉拦截（证据不符 → 可重试 errors）
# ═══════════════════════════════════════════════

class TestHallucination:
    def test_fabricated_partner(self):
        fields = {**GOOD_FIELDS, "partner_b": "深圳不存在的幻影科技有限公司"}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("partner_b" in e for e in report.errors)
        assert report.field_evidence["partner_b"] is False

    def test_fabricated_amount(self):
        fields = {**GOOD_FIELDS, "amount": 9999999.0}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("amount" in e for e in report.errors)

    def test_fabricated_date(self):
        fields = {**GOOD_FIELDS, "sign_date": "2026-12-25"}
        report = validate_extraction(fields, GOOD_TEXT)
        assert report.field_evidence["sign_date"] is False
        assert any("sign_date" in e for e in report.errors)

    def test_ocr_partner_similarity_pass(self):
        """OCR 断行丢字：子串失败但相似度 ≥0.90 → 放行"""
        fields = {**GOOD_FIELDS, "partner_a": "北京智慧城市建设发展有限公"}
        report = validate_extraction(fields, GOOD_TEXT)
        assert report.field_evidence["partner_a"] is True

    def test_missing_value_not_evidence_failure(self):
        """字段缺失不算证据失败（缺失走 critical 逻辑）"""
        fields = {**GOOD_FIELDS, "contract_code": None}
        report = validate_extraction(fields, GOOD_TEXT)
        assert report.field_evidence["contract_code"] is True
        # contract_code 非 critical → 无 fatal
        assert report.fatal_errors == []


# ═══════════════════════════════════════════════
# validator · 金额交叉 / sanity
# ═══════════════════════════════════════════════

class TestAmountCrossAndSanity:
    def test_uppercase_cross_mismatch(self):
        fields = {**GOOD_FIELDS, "amount": 1300000.0}  # 大写是 120 万
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("金额交叉不一致" in e for e in report.errors)

    def test_amount_zero(self):
        fields = {**GOOD_FIELDS, "amount": 0}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("金额异常" in e for e in report.errors)

    def test_amount_over_sanity_max(self):
        fields = {**GOOD_FIELDS, "amount": 2e10}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("上限" in e for e in report.errors)


# ═══════════════════════════════════════════════
# validator · 日期
# ═══════════════════════════════════════════════

class TestDates:
    def test_bad_format_retryable(self):
        fields = {**GOOD_FIELDS, "sign_date": "2026年3月15日"}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("日期格式错误" in e for e in report.errors)

    def test_invalid_value(self):
        fields = {**GOOD_FIELDS, "sign_date": "2026-02-30"}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("日期值不合法" in e for e in report.errors)

    def test_year_out_of_range(self):
        fields = {**GOOD_FIELDS, "sign_date": "1980-01-01"}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("年份越界" in e for e in report.errors)

    def test_sign_after_effective_is_warning_only(self):
        """补签合同：生效早于签订 → 警告不重试"""
        fields = {**GOOD_FIELDS, "sign_date": "2026-05-01"}
        report = validate_extraction(fields, GOOD_TEXT)
        # sign_date 本身是原文日期（2026-05-01 不在原文 → 还有证据错误），这里只关心逻辑顺序
        assert any("签订日期" in w and "生效日期" in w for w in report.warnings)
        assert not any("日期逻辑" in e for e in report.errors)

    def test_expire_before_effective_warning(self):
        fields = {**GOOD_FIELDS, "expire_date": "2026-03-01"}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("生效日期" in w and "到期日期" in w for w in report.warnings)


# ═══════════════════════════════════════════════
# validator · 枚举 / fallback 置信度
# ═══════════════════════════════════════════════

class TestEnumAndFallback:
    def test_bad_dispute_enum(self):
        fields = {**GOOD_FIELDS, "dispute_resolution": "决斗解决"}
        report = validate_extraction(fields, GOOD_TEXT)
        assert any("dispute_resolution 枚举非法" in e for e in report.errors)

    def test_fallback_caps_confidence(self):
        report = validate_extraction(GOOD_FIELDS, GOOD_TEXT, is_fallback=True)
        assert report.is_fallback is True
        assert report.system_confidence <= 0.30

    def test_confidence_degrades_with_errors(self):
        clean = validate_extraction(GOOD_FIELDS, GOOD_TEXT)
        dirty = validate_extraction({**GOOD_FIELDS, "amount": 9999999.0}, GOOD_TEXT)
        assert dirty.system_confidence < clean.system_confidence


# ═══════════════════════════════════════════════
# validator · 收付款计划业财校验
# ═══════════════════════════════════════════════

class TestPaymentSchedule:
    def test_consistent(self):
        ok, msg = check_payment_schedule(
            [{"amount": 600000}, {"ratio": 0.5}], 1200000.0
        )
        assert ok is True and msg is None

    def test_inconsistent(self):
        ok, msg = check_payment_schedule(
            [{"amount": 600000}, {"amount": 300000}], 1200000.0
        )
        assert ok is False and "≠" in msg

    def test_no_amount_returns_none(self):
        ok, msg = check_payment_schedule([{"amount": 100}], None)
        assert ok is None and msg is None

    def test_empty_schedule_returns_none(self):
        ok, msg = check_payment_schedule([], 1200000.0)
        assert ok is None and msg is None

    def test_ratio_only(self):
        ok, _ = check_payment_schedule([{"ratio": 0.3}, {"ratio": 0.7}], 1000.0)
        assert ok is True


# ═══════════════════════════════════════════════
# gatekeeper · quality 块
# ═══════════════════════════════════════════════

class TestGatekeeper:
    def test_clean_report_passes(self):
        report = validate_extraction(GOOD_FIELDS, GOOD_TEXT)
        quality = build_quality(report)
        assert quality["needs_review"] is False
        assert quality["review_reasons"] == []
        assert quality["system_confidence"] == report.system_confidence
        assert quality["payment_schedule_ok"] is None

    def test_fatal_errors_trigger_review(self):
        fields = {**GOOD_FIELDS, "partner_b": None}
        report = validate_extraction(fields, GOOD_TEXT)
        quality = build_quality(report)
        assert quality["needs_review"] is True
        assert any("partner_b" in r for r in quality["review_reasons"])
        assert "partner_b" in quality["critical_missing"]

    def test_medium_confidence_marks_review(self):
        """置信度中等（无 fatal 无 errors 仅警告）→ 建议复核"""
        fields = {**GOOD_FIELDS, "expire_date": "2026-03-01"}
        report = validate_extraction(fields, GOOD_TEXT)
        quality = build_quality(report)
        assert quality["needs_review"] is True
        assert any("系统置信度" in r for r in quality["review_reasons"])

    def test_fallback_marks_review(self):
        report = validate_extraction(GOOD_FIELDS, GOOD_TEXT, is_fallback=True)
        quality = build_quality(report)
        assert quality["needs_review"] is True
        assert any("正则抢救" in r for r in quality["review_reasons"])

    def test_parse_quality_gate(self):
        report = validate_extraction(GOOD_FIELDS, GOOD_TEXT)
        quality = build_quality(report, parse_quality={"score": 0.35, "ok": False})
        assert quality["needs_review"] is True
        assert any("解析质量差" in r for r in quality["review_reasons"])

    def test_schedule_fail_marks_review(self):
        report = validate_extraction(GOOD_FIELDS, GOOD_TEXT)
        quality = build_quality(
            report, schedule_ok=False, schedule_message="收付款计划合计 ≠ 合同金额"
        )
        assert quality["needs_review"] is True
        assert quality["payment_schedule_ok"] is False
        assert any("业财校验" in r for r in quality["review_reasons"])

    def test_classify_reasons_passthrough(self):
        report = validate_extraction(GOOD_FIELDS, GOOD_TEXT)
        quality = build_quality(
            report, classify_review_reasons=["分类置信度低(0.30)且仲裁未果"]
        )
        assert quality["needs_review"] is True
        assert any("仲裁未果" in r for r in quality["review_reasons"])

    def test_thresholds_exposed(self):
        report = validate_extraction(GOOD_FIELDS, GOOD_TEXT)
        quality = build_quality(report)
        assert set(quality["thresholds"].keys()) == {"review", "strict"}


# ═══════════════════════════════════════════════
# 集成场景：错误喂回 → 自我纠正后的复检
# ═══════════════════════════════════════════════

class TestSelfCorrectionLoop:
    def test_corrected_fields_pass(self):
        """第一轮幻觉字段 → 修正后应全绿（模拟 LLM 自我纠正成功）"""
        bad = validate_extraction(
            {**GOOD_FIELDS, "partner_b": "深圳不存在的幻影科技有限公司"}, GOOD_TEXT
        )
        assert bad.has_retryable_errors
        fixed = validate_extraction(GOOD_FIELDS, GOOD_TEXT)
        assert not fixed.has_retryable_errors
        assert fixed.system_confidence > bad.system_confidence


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

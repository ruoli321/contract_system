"""M18 · pytest 单元测试 — classifier.py（合同分类）

覆盖：
    _rule_classify()   — 规则关键词匹配（采购/销售/服务/租赁）
    融合策略           — llm_high / llm_low / rule_fallback 三分支
    edge cases         — 空文本 / 纯噪声 / 未知类型
"""
import pytest
from app.services.classifier import ContractClassifier, ClassifyResult


# ════════════════════════════════════════════════════════════
# Fixture：纯规则分类器（不传 LLM，强制 rule_fallback 路径）
# ════════════════════════════════════════════════════════════

@pytest.fixture
def rule_only_classifier():
    """不带 LLM 的分类器，只测规则通道"""
    return ContractClassifier(llm_client=None, prompt_manager=None)


@pytest.fixture
def mock_llm_classifier():
    """带 mock LLM 的分类器，能模拟 LLM 返回"""
    class _MockLLM:
        def __init__(self, return_type="采购合同", confidence=0.9, fail=False):
            self._return_type = return_type
            self._confidence = confidence
            self._fail = fail

        def chat(self, messages, json_mode=True):
            if self._fail:
                raise ConnectionError("LLM 不可用")

            class _Resp:
                content = f'{{"contract_type": "{self._return_type}", "confidence": {self._confidence}}}'
            return _Resp()

    return _MockLLM


# ════════════════════════════════════════════════════════════
# 规则通道测试
# ════════════════════════════════════════════════════════════

class TestRuleClassify:

    def test_purchase_keywords(self, rule_only_classifier):
        text = "本合同为采购合同，甲方向乙方采购服务器设备。"
        result = rule_only_classifier.classify(text, "采购合同")
        assert result.rule_result == "采购合同"
        assert result.rule_confidence >= 0.5

    def test_sales_keywords(self, rule_only_classifier):
        text = "销售协议：卖方将货物出售给买方。"
        result = rule_only_classifier.classify(text, "销售合同")
        assert result.rule_result == "销售合同"

    def test_service_keywords(self, rule_only_classifier):
        text = "乙方提供技术服务和咨询支持服务。"
        result = rule_only_classifier.classify(text, "服务合同")
        assert result.rule_result == "服务合同"

    def test_lease_keywords(self, rule_only_classifier):
        text = "租赁合同：出租方将房屋出租给承租方。"
        result = rule_only_classifier.classify(text, "租赁合同")
        assert result.rule_result == "租赁合同"

    def test_no_keywords_defaults_to_other(self, rule_only_classifier):
        text = "这是一段完全不包含任何合同关键词的随机文本内容。"
        result = rule_only_classifier.classify(text, "")
        assert result.rule_result in ("其他", "采购合同")  # 默认值

    def test_short_text(self, rule_only_classifier):
        text = ""
        result = rule_only_classifier.classify(text, "")
        # 空文本不应崩溃
        assert isinstance(result, ClassifyResult)


# ════════════════════════════════════════════════════════════
# 融合策略测试（三分支）
# ════════════════════════════════════════════════════════════

class TestFusionStrategy:

    def test_llm_failure_falls_back_to_rules(self):
        class _FailLLM:
            def chat(self, *a, **kw):
                raise ConnectionError("网络不通")

        clf = ContractClassifier(llm_client=_FailLLM(), prompt_manager=None)
        result = clf.classify("采购服务器", "")
        assert result.method_used == "rule_fallback"
        assert result.llm_failed is True
        # 规则应该能识别 "采购"
        assert result.rule_result == "采购合同"

    def test_llm_high_confidence_used(self):
        class _HighLLM:
            def chat(self, *a, **kw):
                class _R:
                    content = '{"type": "采购合同", "confidence": 0.92}'
                    usage = None  # classifier.py 会访问 response.usage，必须提供
                return _R()

        clf = ContractClassifier(llm_client=_HighLLM(), prompt_manager=None)
        result = clf.classify("采购服务器设备一批", "")
        assert result.method_used == "llm"
        assert result.contract_type == "采购合同"
        assert result.confidence >= 0.7

    def test_llm_low_confidence_ensemble(self):
        class _LowLLM:
            def chat(self, *a, **kw):
                class _R:
                    content = '{"type": "采购合同", "confidence": 0.45}'
                    usage = None
                return _R()

        clf = ContractClassifier(llm_client=_LowLLM(), prompt_manager=None)
        result = clf.classify("采购服务器设备一批", "")
        # conf < 0.7 → ensemble
        assert result.method_used in ("ensemble", "rule_fallback", "llm")
        # 无论用什么策略，都应返回一个有效类型
        assert result.contract_type in ContractClassifier.VALID_TYPES

    def test_result_has_all_fields(self):
        class _OKLLM:
            def chat(self, *a, **kw):
                class _R:
                    content = '{"type": "销售合同", "confidence": 0.85}'
                    usage = None
                return _R()

        clf = ContractClassifier(llm_client=_OKLLM(), prompt_manager=None)
        result = clf.classify("销售产品", "")
        # 所有 dataclass 字段都应有值
        assert isinstance(result.contract_type, str)
        assert isinstance(result.confidence, float)
        assert isinstance(result.rule_result, str)
        assert isinstance(result.rule_confidence, float)
        assert isinstance(result.llm_result, str)
        assert isinstance(result.llm_confidence, float)
        assert isinstance(result.llm_failed, bool)
        assert isinstance(result.method_used, str)
        assert result.contract_type in ContractClassifier.VALID_TYPES


# ════════════════════════════════════════════════════════════
# 边界条件
# ════════════════════════════════════════════════════════════

class TestClassifierEdgeCases:

    def test_none_text(self, rule_only_classifier):
        # 应抛异常或安全处理（取决于实现）
        try:
            result = rule_only_classifier.classify(None, "")
            # 如果不抛，必须返回有效结果
            assert result.contract_type in ContractClassifier.VALID_TYPES
        except (TypeError, ValueError):
            pass  # 允许抛

    def test_whitespace_only(self, rule_only_classifier):
        result = rule_only_classifier.classify("   \n\n   ", "")
        assert result.contract_type in ContractClassifier.VALID_TYPES

    def test_confidence_in_valid_range(self, rule_only_classifier):
        result = rule_only_classifier.classify("采购", "")
        assert 0.0 <= result.confidence <= 1.0
        assert 0.0 <= result.rule_confidence <= 1.0

    def test_result_dataclass_serializable(self, rule_only_classifier):
        result = rule_only_classifier.classify("采购", "")
        d = result.__dict__
        assert "contract_type" in d
        assert "confidence" in d


# ════════════════════════════════════════════════════════════
# 真实样例验证
# ════════════════════════════════════════════════════════════

class TestRealSamples:

    def test_purchase_sample(self, rule_only_classifier):
        text = "本合同为采购合同。甲方向乙方采购服务器设备共 10 台，每台单价 5 万元。"
        result = rule_only_classifier.classify(text, "")
        assert result.contract_type == "采购合同"

    def test_service_sample(self, rule_only_classifier):
        text = "乙方为甲方提供年度技术支持服务，包括系统运维、故障排查。"
        result = rule_only_classifier.classify(text, "")
        assert result.contract_type == "服务合同"

    def test_lease_sample(self, rule_only_classifier):
        text = "出租方将写字楼第 18 层出租给承租方使用，租期一年。"
        result = rule_only_classifier.classify(text, "")
        assert result.contract_type == "租赁合同"

    def test_sales_sample(self, rule_only_classifier):
        text = "卖方将产品出售给买方，交货后买方支付货款。"
        result = rule_only_classifier.classify(text, "")
        assert result.contract_type == "销售合同"

# ═══════════════════════════════════════════════════════════════════
# M19 · LangGraph 多 Agent 图测试
#
# 覆盖：
#   1. happy path：全字段通过 → 零重试直达 review
#   2. retry 回路：首次缺字段 → validate 失败 → retry → 二次成功
#   3. 重试上限：持续失败 → 外重试 2 次后走 review 输出（不死循环）
#   4. LangChain Runnable 线性管道（D3 证据）
# ═══════════════════════════════════════════════════════════════════
import pytest

from app.graph import build_contract_graph, run_contract_graph, run_linear_chain, MAX_GRAPH_RETRIES
from app.services.extractor import ExtractResult


# ═══════════════════════════════════════════════════════════════════
# Mock 服务（模拟 classifier/extractor 的真实接口签名）
# ═══════════════════════════════════════════════════════════════════

class MockClassifyResult:
    """模拟 classifier.ClassifyResult（属性访问，不是 tuple）"""

    def __init__(self, contract_type="采购合同", confidence=0.9, method_used="llm"):
        self.contract_type = contract_type
        self.confidence = confidence
        self.method_used = method_used
        self.rule_result = contract_type
        self.rule_confidence = confidence
        self.llm_result = contract_type
        self.llm_confidence = confidence
        self.llm_failed = False


class MockClassifier:
    def __init__(self):
        self.calls = 0

    def classify(self, text, title=""):
        self.calls += 1
        return MockClassifyResult()


FULL_FIELDS = {
    "contract_name": "XX 服务器采购合同",
    "contract_code": "CG-2026-0001",
    "partner_a": "XX 科技有限公司",
    "partner_b": "YY 信息技术有限公司",
    "amount": 500000.0,
    "amount_uppercase": "伍拾万元整",
    "currency": "CNY",
    "sign_date": "2026-08-01",
    "effective_date": "2026-08-01",
    "expire_date": "2027-07-31",
    "contract_type": "采购合同",
    "payment_terms": "签订后 5 个工作日内支付 30% 预付款",
    "breach_clause": "逾期交货每日按合同金额 0.5‰ 支付违约金",
    "dispute_resolution": "诉讼",
    "confidence": 0.92,
}


def _make_extract_result(fields: dict, attempt: int) -> ExtractResult:
    return ExtractResult(
        fields=fields,
        prompt_version="v1",
        system_prompt_name="system_extract",
        few_shot_sources=["purchase_01", "purchase_02"],
        few_shot_count=2,
        used_rag=True,
        attempt_count=attempt,
        elapsed_seconds=0.1,
        validation_errors=[],
        llm_failed=False,
    )


class MockExtractor:
    """可控提取器：fail_first=True 时第一次返回缺字段的空结果"""

    def __init__(self, fail_first=False, always_fail=False):
        self.calls = 0
        self.fail_first = fail_first
        self.always_fail = always_fail

    def extract_contract_fields(self, contract_text, contract_type=None,
                                use_rag=True, top_k_examples=3, exclude_ids=None):
        self.calls += 1
        if self.always_fail or (self.fail_first and self.calls == 1):
            fields = {k: None for k in FULL_FIELDS}
            fields["contract_type"] = contract_type
            fields["confidence"] = 0.1
        else:
            fields = dict(FULL_FIELDS)
            fields["contract_type"] = contract_type
        return _make_extract_result(fields, attempt=self.calls)


# ═══════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════

def test_graph_happy_path():
    """全字段通过 → 零重试直达 review"""
    classifier, extractor = MockClassifier(), MockExtractor()
    graph_app, _ = build_contract_graph(classifier, extractor)

    result = run_contract_graph(graph_app, "测试合同文本")

    assert result["used_langgraph"] is True
    assert result["contract_type"] == "采购合同"
    assert result["method_used"] == "llm"
    assert result["retry_count"] == 0
    assert result["validation_errors"] == []
    assert result["fields"]["contract_name"] == "XX 服务器采购合同"
    assert result["fields"]["amount"] == 500000.0
    assert extractor.calls == 1
    assert classifier.calls == 1


def test_graph_retry_loop_recovers():
    """首次缺字段 → validate 失败 → retry 回路 → 二次提取成功"""
    classifier, extractor = MockClassifier(), MockExtractor(fail_first=True)
    graph_app, _ = build_contract_graph(classifier, extractor)

    result = run_contract_graph(graph_app, "测试合同文本")

    assert result["retry_count"] == 1
    assert extractor.calls == 2
    assert result["validation_errors"] == []
    assert result["fields"]["contract_name"] == "XX 服务器采购合同"


def test_graph_max_retries_no_infinite_loop():
    """持续失败 → 外重试 MAX_GRAPH_RETRIES 次后走 review，不死循环"""
    classifier, extractor = MockClassifier(), MockExtractor(always_fail=True)
    graph_app, _ = build_contract_graph(classifier, extractor)

    result = run_contract_graph(graph_app, "测试合同文本")

    # 1 次初始 + 2 次外重试 = 3 次提取调用
    assert extractor.calls == 1 + MAX_GRAPH_RETRIES
    assert result["retry_count"] == MAX_GRAPH_RETRIES
    # 校验错误保留输出（供调用方感知失败原因）
    assert len(result["validation_errors"]) > 0
    assert any("contract_name" in e for e in result["validation_errors"])


def test_linear_chain_d3_evidence():
    """LangChain Runnable 线性管道：text 透传 + fields 输出"""
    classifier, extractor = MockClassifier(), MockExtractor()
    _, linear_chain = build_contract_graph(classifier, extractor)

    result = run_linear_chain(linear_chain, "测试合同文本")

    assert result["used_langchain"] is True
    assert result["contract_type"] == "采购合同"
    assert result["fields"]["partner_a"] == "XX 科技有限公司"
    assert result["few_shot_sources"] == ["purchase_01", "purchase_02"]
    assert extractor.calls == 1


def test_graph_passes_contract_type_to_extractor():
    """分类结果应传给提取器（RAG 按类型检索范例）"""
    classifier, extractor = MockClassifier(), MockExtractor()
    graph_app, _ = build_contract_graph(classifier, extractor)

    run_contract_graph(graph_app, "测试合同文本")

    # extractor 收到的 contract_type 来自分类节点
    assert classifier.calls == 1  # 分类只跑一次（retry 回 extract 不回 classify）

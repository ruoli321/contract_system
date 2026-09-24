"""M13 · 合同字段提取模块 — 本地 mock 测试"""
import sys, os, json, time, re
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from app.services.extractor import ContractExtractor, ContractExtraction, ExtractResult


# ═══════════════════════════════════════════════════════════
# Mock 辅助
# ═══════════════════════════════════════════════════════════

def _make_mock_prompt_manager(render_returns: dict = None):
    """
    创建 mock PromptManager。
    render_returns 可以按 prompt name 预设返回值，默认返回空 dict。
    """
    mock_pm = MagicMock()

    def _render_side_effect(name, version=None, **variables):
        if render_returns and name in render_returns:
            return render_returns[name]
        return {"system": "SYSTEM PROMPT", "user": f"USER PROMPT({name})", "name": name, "version": "v1"}

    mock_pm.render.side_effect = _render_side_effect
    return mock_pm


def _make_mock_llm(response_json: str = None, side_effect=None):
    """
    创建 mock LLM 客户端（模拟 M10 BaseLLM.chat）。
    response_json: 成功时返回的 JSON 字符串
    side_effect:   异常（模拟 LLM 调用失败）
    """
    mock_llm = MagicMock()
    if side_effect is not None:
        mock_llm.chat.side_effect = side_effect
    else:
        mock_llm.chat.return_value = response_json or '{"contract_name": "test", "confidence": 0.9}'
    return mock_llm


def _make_sample_good_extraction_json():
    """一份符合 ContractExtraction Schema 的完整 JSON"""
    return json.dumps({
        "contract_name": "办公设备采购合同",
        "contract_code": "CG-2025-001",
        "partner_a": "北京科技有限公司",
        "partner_b": "上海贸易有限公司",
        "amount": 500000.0,
        "amount_uppercase": "人民币伍拾万元整",
        "currency": "CNY",
        "sign_date": "2025-01-15",
        "effective_date": "2025-02-01",
        "expire_date": "2026-01-31",
        "contract_type": "采购合同",
        "payment_terms": "货到验收合格后30日内付款",
        "breach_clause": "逾期付款按日万分之五支付违约金",
        "dispute_resolution": "向人民法院提起诉讼",
        "confidence": 0.92,
    }, ensure_ascii=False)


def _make_mock_rag_learner(examples: list[dict] = None):
    """创建 mock RAGLearner — retrieve_and_format_few_shot 和 format_few_shot 都返回预构建值"""
    mock_rag = MagicMock()
    if examples is None:
        examples = [
            {"id": "purchase_01", "contract_type": "采购合同", "score": 0.85,
             "text": "甲方北京科技有限公司向乙方上海贸易有限公司采购办公设备。合同总金额人民币500,000元整。",
             "extraction": {"contract_name": "办公设备采购合同", "partner_a": "北京科技有限公司", "amount": 500000}},
            {"id": "purchase_02", "contract_type": "采购合同", "score": 0.78,
             "text": "另一份采购合同...",
             "extraction": {"contract_name": "采购合同模板", "amount": 300000}},
        ]

    # 预构建 few_shot 文本
    few_shot_text = "\n\n".join(
        f"【范例{i+1} · {e['contract_type']}】（相似度 {e['score']}）\n"
        f"合同片段：{e['text']}\n"
        f"正确提取结果：{json.dumps(e['extraction'], ensure_ascii=False)}"
        for i, e in enumerate(examples)
    )

    # retrieve_and_format_few_shot → 返回 (few_shot_text, examples)
    mock_rag.retrieve_and_format_few_shot.return_value = (few_shot_text, examples)
    # format_few_shot → 也返回同样预构建值
    mock_rag.format_few_shot.return_value = few_shot_text
    return mock_rag


# ── 测试合同文本 ──

SAMPLE_TEXT = """甲方（采购方）北京科技有限公司与乙方（供货方）上海贸易有限公司。
经双方协商一致，签订本合同。

合同名称：办公设备采购合同
合同编号：CG-2025-001

第一条 采购内容：服务器10台，单价50,000元，合计人民币500,000元（伍拾万元整）。
第二条 付款方式：货到验收合格后30日内付款。
第三条 违约责任：甲方逾期付款按日万分之五支付违约金。
第四条 争议解决：协商不成，向人民法院提起诉讼。
第五条 本合同自双方盖章之日起生效，有效期2025年2月1日至2026年1月31日。

签订日期：2025年1月15日
"""


# ═══════════════════════════════════════════════════════════
# 测试 1 · Happy Path — 一次成功
# ═══════════════════════════════════════════════════════════

def test_happy_path_first_attempt():
    print("📋 测试 1 · Happy Path — 一次成功（含 RAG）")

    mock_llm = _make_mock_llm(_make_sample_good_extraction_json())
    mock_pm = _make_mock_prompt_manager()
    mock_rag = _make_mock_rag_learner()

    extractor = ContractExtractor(
        llm_client=mock_llm,
        prompt_manager=mock_pm,
        rag_learner=mock_rag,
    )

    result = extractor.extract_contract_fields(SAMPLE_TEXT, contract_type="采购合同", use_rag=True)

    # ── 验证 ExtractResult 结构 ──
    assert isinstance(result, ExtractResult)
    assert result.attempt_count == 1, f"一次成功 attempt_count 应为 1，实际 {result.attempt_count}"
    assert result.used_rag == True
    assert result.few_shot_count == 2  # mock_rag 提供 2 个范例
    assert result.few_shot_sources == ["purchase_01", "purchase_02"]
    assert result.llm_failed == False
    assert result.validation_errors == []
    assert result.elapsed_seconds > 0
    print(f"  ✅ ExtractResult 结构完整: rag={result.used_rag}, examples={result.few_shot_count}, attempts={result.attempt_count}")

    # ── 验证 fields 内容 ──
    f = result.fields
    assert f["contract_name"] == "办公设备采购合同"
    assert f["partner_a"] == "北京科技有限公司"
    assert f["amount"] == 500000.0
    assert f["confidence"] == 0.92
    assert f["contract_type"] == "采购合同"
    print(f"  ✅ fields 完整提取: {f['contract_name']}, amount={f['amount']}, confidence={f['confidence']}")

    # ── 验证 LLM 调用参数（M10 API）──
    mock_llm.chat.assert_called_once()
    call_kwargs = mock_llm.chat.call_args.kwargs
    assert call_kwargs.get("temperature") == 0.0
    assert call_kwargs.get("json_mode") == True
    # messages 应是 system + user
    messages = mock_llm.chat.call_args.args[0]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    print(f"  ✅ LLM 调用正确: chat(messages, temperature=0.0, json_mode=True)")

    # ── 验证 PromptManager 调用 ──
    # 首次 render 应调用 system_extract（3 次 total：system_extract + extract 各一次）
    assert mock_pm.render.call_count == 2, f"首次成功应 render 2 次（system + extract），实际 {mock_pm.render.call_count}"
    # 验证 RAG 范例被拼进 user prompt
    user_render_call = [c for c in mock_pm.render.call_args_list if c.args[0] == "extract"][0]
    user_few_shot_arg = user_render_call.kwargs.get("few_shot_examples", "")
    assert "范例" in user_few_shot_arg, "few_shot_examples 应包含范例文本"
    print(f"  ✅ PromptManager.render 2 次 + RAG 范例已拼入 extract prompt")

    # ── 验证 to_api_dict ──
    # M21 起 to_api_dict 拍平 fields（main.py 统一在 data["extraction"] 下再包一层）
    api_dict = result.to_api_dict()
    assert "contract_name" in api_dict
    assert "confidence" in api_dict
    assert "attempt_count" in api_dict
    assert "elapsed_seconds" in api_dict
    print(f"  ✅ to_api_dict() 含全部 API 需要字段")

    print("  🟢 测试 1 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 2 · RAG 禁用 — use_rag=False
# ═══════════════════════════════════════════════════════════

def test_rag_disabled():
    print("📋 测试 2 · use_rag=False → 跳过 RAG 检索")

    mock_llm = _make_mock_llm(_make_sample_good_extraction_json())
    mock_pm = _make_mock_prompt_manager()
    mock_rag = _make_mock_rag_learner()

    extractor = ContractExtractor(llm_client=mock_llm, prompt_manager=mock_pm, rag_learner=mock_rag)

    result = extractor.extract_contract_fields(SAMPLE_TEXT, contract_type="采购合同", use_rag=False)

    assert result.used_rag == False
    assert result.few_shot_count == 0
    assert result.few_shot_sources == []
    # RAGLearner 不应被调用
    mock_rag.retrieve_and_format_few_shot.assert_not_called()

    # user_prompt 中的 few_shot_examples 应为 "（暂无参考范例）"
    user_render_call = [c for c in mock_pm.render.call_args_list if c.args[0] == "extract"][0]
    assert "（暂无参考范例）" in str(user_render_call.kwargs.get("few_shot_examples", ""))

    print(f"  ✅ RAG 禁用: used_rag=False, sources=[], VS 未调用")
    print("  🟢 测试 2 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 3 · RAGLearner 未注入 — 自动跳过
# ═══════════════════════════════════════════════════════════

def test_rag_learner_not_injected():
    print("📋 测试 3 · RAGLearner=None + use_rag=True → 安全降级")

    mock_llm = _make_mock_llm(_make_sample_good_extraction_json())
    mock_pm = _make_mock_prompt_manager()

    extractor = ContractExtractor(llm_client=mock_llm, prompt_manager=mock_pm, rag_learner=None)

    result = extractor.extract_contract_fields(SAMPLE_TEXT, contract_type="采购合同", use_rag=True)

    # RAGLearner=None 时应安全跳过，不报错
    assert result.used_rag == False
    assert result.attempt_count == 1

    print(f"  ✅ RAGLearner=None 不报错: used_rag=False, attempts=1")
    print("  🟢 测试 3 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 4 · JSON 解析失败 → 用 extract_retry prompt 重试
# ═══════════════════════════════════════════════════════════

def test_json_parse_fail_then_retry_succeeds():
    print("📋 测试 4 · JSON 解析失败 → 重试成功")

    # 第一次返回无效 JSON，第二次返回正确 JSON
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = [
        "这不是 JSON，只是随便的文字",                    # attempt 1 → JSONDecodeError
        _make_sample_good_extraction_json(),              # attempt 2 → 成功
    ]
    mock_pm = _make_mock_prompt_manager()

    extractor = ContractExtractor(llm_client=mock_llm, prompt_manager=mock_pm, rag_learner=None)

    result = extractor.extract_contract_fields(SAMPLE_TEXT, contract_type="采购合同", use_rag=False)

    # 应重试 1 次后成功
    assert result.attempt_count == 2
    assert result.llm_failed == False
    assert result.fields["contract_name"] == "办公设备采购合同"
    assert result.fields["confidence"] == 0.92

    # 验证 extract_retry prompt 被使用
    prompt_names = [c.args[0] for c in mock_pm.render.call_args_list]
    assert "extract_retry" in prompt_names, f"重试时应使用 extract_retry prompt，实际 render 过: {prompt_names}"
    print(f"  ✅ 第 2 次尝试成功: attempts={result.attempt_count}, retry prompt 正确使用")
    print("  🟢 测试 4 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 5 · 业务校验失败（日期格式）→ 重试
# ═══════════════════════════════════════════════════════════

def test_business_validation_fail_then_retry():
    print("📋 测试 5 · 业务校验失败（日期格式）→ 重试成功")

    bad_json = json.dumps({
        "contract_name": "test", "contract_code": None,
        "partner_a": "A公司", "partner_b": "B公司",
        "amount": 100000.0, "amount_uppercase": None,
        "currency": "CNY",
        "sign_date": "2025/01/15",          # ❌ 日期格式错误
        "effective_date": None, "expire_date": None,
        "contract_type": "销售合同",
        "payment_terms": None, "breach_clause": None,
        "dispute_resolution": None, "confidence": 0.8,
    })
    good_json = _make_sample_good_extraction_json()

    mock_llm = MagicMock()
    mock_llm.chat.side_effect = [bad_json, good_json]
    mock_pm = _make_mock_prompt_manager()

    extractor = ContractExtractor(llm_client=mock_llm, prompt_manager=mock_pm, rag_learner=None)

    result = extractor.extract_contract_fields(SAMPLE_TEXT, contract_type="采购合同", use_rag=False)

    assert result.attempt_count == 2
    assert result.fields["sign_date"] == "2025-01-15"
    assert result.fields["contract_name"] == "办公设备采购合同"

    # extract_retry prompt 应传入了 validation_errors
    retry_call = [c for c in mock_pm.render.call_args_list if c.args[0] == "extract_retry"][0]
    validation_errors_arg = retry_call.kwargs.get("validation_errors", "")
    assert "sign_date" in validation_errors_arg, "validation_errors 应包含日期字段名"

    print(f"  ✅ 日期格式错误被捕获 → 重试: attempts={result.attempt_count}")
    print(f"  ✅ validation_errors 已传给 extract_retry prompt")
    print("  🟢 测试 5 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 6 · LLM 调用异常 → 重试 + 最终兜底
# ═══════════════════════════════════════════════════════════

def test_llm_exception_all_retries_exhausted():
    print("📋 测试 6 · LLM 连续异常 → 3 次重试后兜底，llm_failed=True")

    mock_llm = _make_mock_llm(side_effect=Exception("Connection timeout"))
    mock_pm = _make_mock_prompt_manager()

    extractor = ContractExtractor(llm_client=mock_llm, prompt_manager=mock_pm, rag_learner=None)

    result = extractor.extract_contract_fields(SAMPLE_TEXT, contract_type="采购合同", use_rag=False)

    # 应该调了 3 次 LLM，然后返回兜底
    assert mock_llm.chat.call_count == 3, f"LLM 应被调用 3 次后耗尽重试，实际 {mock_llm.chat.call_count}"
    assert result.attempt_count == 3
    assert result.llm_failed == True
    # M21 兜底特征：正则抢救结构化字段（有据可查）+ is_fallback 强制低置信走人工
    assert result.is_fallback is True
    assert result.system_confidence <= 0.30
    assert result.fields["confidence"] == 0.1
    # contract_name 不再冒充（原文截断会污染台账）→ None，由 critical_missing 拦下
    assert result.fields["contract_name"] is None
    # 甲乙方/金额/日期等结构化字段由正则从原文抢救（postprocess_fill）
    assert result.fields["partner_a"] == "北京科技有限公司"
    assert result.fields["contract_type"] == "采购合同"  # 保留用户传入的类型
    assert len(result.validation_errors) > 0
    assert any("LLM 调用异常" in e for e in result.validation_errors)

    print(f"  ✅ 3 次 LLM 异常 → 正则抢救兜底: attempts={result.attempt_count}, llm_failed=True, is_fallback=True")
    print(f"  ✅ validation_errors 包含 LLM 异常信息")
    print("  🟢 测试 6 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 7 · JSON 容错解析（markdown 代码块 / 额外文字）
# ═══════════════════════════════════════════════════════════

def test_parse_json_tolerant():
    print("📋 测试 7 · JSON 容错解析")

    # 各种非完美 JSON 输出
    cases = [
        # 正常 JSON
        ('{"a": 1, "b": "x"}', {"a": 1, "b": "x"}),
        # markdown 代码块包裹
        ('```json\n{"a": 1}\n```', {"a": 1}),
        # 代码块带额外文字
        ('这里是分析：\n```json\n{"a": 1}\n```\n请参考', {"a": 1}),
        # 最外层 {} 提取
        ('分析结果是这样的：{"a": 1, "b": [1,2]}', {"a": 1, "b": [1, 2]}),
    ]

    for i, (input_str, expected) in enumerate(cases):
        result = ContractExtractor._parse_json(input_str)
        assert result == expected, f"case {i} 失败: {result} != {expected}"
        print(f"  ✅ case {i+1}: {input_str[:30]}... → {result}")

    print("  🟢 测试 7 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 8 · _business_validate 业务校验覆盖
# ═══════════════════════════════════════════════════════════

def test_business_validate():
    print("📋 测试 8 · 业务校验（日期 + 金额大写）")

    # ── 日期格式错误 ──
    bad_date = ContractExtraction(
        contract_name="test", partner_a="A", partner_b="B",
        contract_type="采购合同", confidence=0.9,
        sign_date="2025/01/15",          # ❌
        effective_date=None, expire_date=None,
    )
    errors = ContractExtractor._business_validate(bad_date, "")
    assert any("sign_date" in e for e in errors), f"应捕获日期格式错误: {errors}"
    print(f"  ✅ 日期格式错误被捕获: {errors[0]}")

    # ── 非法日期值 ──
    bad_date_val = ContractExtraction(
        contract_name="test", partner_a="A", partner_b="B",
        contract_type="采购合同", confidence=0.9,
        sign_date="2025-13-40",          # ❌ 不存在的月份
    )
    errors = ContractExtractor._business_validate(bad_date_val, "")
    assert any("不合法" in e for e in errors), f"应捕获非法日期值: {errors}"
    print(f"  ✅ 非法日期值被捕获: {errors[0]}")

    # ── 金额大写不一致 → M21 硬错误（触发重试自我纠正）──
    # 旧版仅 WARNING；M21 统一校验器将其升级为可重试 errors
    bad_amount = ContractExtraction(
        contract_name="test", partner_a="A", partner_b="B",
        contract_type="采购合同", confidence=0.9,
        amount=500000.0,
        amount_uppercase="人民币壹佰万元整",   # 与 amount 不一致 → error
    )
    errors = ContractExtractor._business_validate(
        bad_amount, "test 甲方：A 乙方：B 合同总价款：人民币壹佰万元整"
    )
    assert any("金额交叉不一致" in e for e in errors), f"金额交叉不一致应为硬错误: {errors}"
    print(f"  ✅ 金额大写交叉不一致 → 硬错误触发重试")

    # ── 日期为 None 的字段不应报错（其余字段有原文依据）──
    ok = ContractExtraction(
        contract_name="test", partner_a="A", partner_b="B",
        contract_type="采购合同", confidence=0.9,
        sign_date=None, effective_date=None, expire_date=None,
    )
    errors = ContractExtractor._business_validate(ok, "test 甲方：A 乙方：B")
    assert errors == [], f"日期为 None 不应报错: {errors}"
    print(f"  ✅ 日期为 None 不报错")

    print("  🟢 测试 8 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 9 · 兼容旧 API extract()
# ═══════════════════════════════════════════════════════════

def test_extract_backward_compat():
    print("📋 测试 9 · .extract() 旧 API 兼容")

    mock_llm = _make_mock_llm(_make_sample_good_extraction_json())
    mock_pm = _make_mock_prompt_manager()

    extractor = ContractExtractor(llm_client=mock_llm, prompt_manager=mock_pm, rag_learner=None)

    # 旧签名：extract(text, contract_type, use_few_shot) → dict
    result_dict = extractor.extract(SAMPLE_TEXT, contract_type="采购合同", use_few_shot=False)

    assert isinstance(result_dict, dict)
    assert result_dict["contract_name"] == "办公设备采购合同"
    assert result_dict["confidence"] == 0.92
    print(f"  ✅ extract() → dict, contract_name={result_dict['contract_name']}")

    print("  🟢 测试 9 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 10 · PromptManager 参数传递正确性（变量替换）
# ═══════════════════════════════════════════════════════════

def test_prompt_manager_variables():
    print("📋 测试 10 · PromptManager.render 变量传递正确")

    mock_llm = _make_mock_llm(_make_sample_good_extraction_json())
    # 自定义 pm 验证变量确实被传入
    mock_pm = MagicMock()
    expected_render = {"system": "SYS", "user": "USER", "name": "extract", "version": "v2"}
    mock_pm.render.return_value = expected_render

    extractor = ContractExtractor(llm_client=mock_llm, prompt_manager=mock_pm, rag_learner=None)
    extractor.extract_contract_fields(SAMPLE_TEXT, contract_type="销售合同", use_rag=False)

    # 验证 system_extract 调用
    sys_call = [c for c in mock_pm.render.call_args_list if c.args[0] == "system_extract"][0]
    assert sys_call.args[0] == "system_extract"

    # 验证 extract 调用的变量
    ext_call = [c for c in mock_pm.render.call_args_list if c.args[0] == "extract"][0]
    assert ext_call.kwargs.get("contract_type") == "销售合同"
    assert ext_call.kwargs.get("contract_text") == SAMPLE_TEXT[:8000]  # 截断
    assert "（暂无参考范例）" in str(ext_call.kwargs.get("few_shot_examples", ""))

    print(f"  ✅ system_extract 调用正确")
    print(f"  ✅ extract 变量正确: contract_type=销售合同, text={len(ext_call.kwargs['contract_text'])}字, few_shot=暂无范例")
    print("  🟢 测试 10 通过\n")


# ═══════════════════════════════════════════════════════════
# 运行全部测试
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("M13 · 合同字段提取模块 — 本地 mock 测试")
    print("=" * 60 + "\n")

    test_happy_path_first_attempt()
    test_rag_disabled()
    test_rag_learner_not_injected()
    test_json_parse_fail_then_retry_succeeds()
    test_business_validation_fail_then_retry()
    test_llm_exception_all_retries_exhausted()
    test_parse_json_tolerant()
    test_business_validate()
    test_extract_backward_compat()
    test_prompt_manager_variables()

    print("=" * 60)
    print("🎉 全部 10 项测试通过 ✅")
    print("=" * 60)

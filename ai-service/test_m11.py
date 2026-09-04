"""M11 · 合同分类模块 — 本地 mock 测试（不调真实 API）"""
import sys, os, json
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from app.services.classifier import ContractClassifier, ClassifyResult
from app.services.llm_client import LLMResponse


# ═══════════════════════════════════════════════════════════
# Mock 辅助函数
# ═══════════════════════════════════════════════════════════

def _make_mock_llm(content: str, usage=None):
    """创建一个 mock LLM，chat() 返回指定 content"""
    mock_llm = MagicMock()
    mock_llm.chat.return_value = LLMResponse(
        content=content,
        usage=usage or {"prompt_tokens": 100, "completion_tokens": 20},
        model="deepseek-chat",
    )
    return mock_llm


def _make_mock_llm_raise(exception_class=Exception, msg="LLM timeout"):
    """创建一个 mock LLM，chat() 直接抛异常"""
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = exception_class(msg)
    return mock_llm


# ── 四类合同的典型文本片段（用于规则匹配测试）──

CONTRACT_TEXTS = {
    "采购合同": """
    采购合同
    甲方（采购方）：北京科技有限公司
    乙方（供货方）：上海设备供应商
    经双方协商一致，甲方同意向乙方采购以下设备：
    1. 服务器 10 台，单价 50,000 元
    2. 网络设备一批，金额 200,000 元
    合同总金额人民币 700,000 元整。
    采购订单编号：PO-2025-001
    交货地点：甲方指定仓库
    付款方式：货到验收合格后 30 日内付款
    """,
    "销售合同": """
    销售合同
    甲方（销售方/供方）：广州产品有限公司
    乙方（买方/客户）：深圳贸易集团
    甲方同意向乙方销售下列产品：
    产品名称 A，数量 1000 件，单价 100 元
    产品名称 B，数量 500 件，单价 200 元
    合同总金额人民币 200,000 元整。
    销售协议编号：SA-2025-008
    发货地点：甲方工厂
    """,
    "服务合同": """
    技术服务协议
    甲方（委托方）：某大型集团公司
    乙方（服务方）：专业咨询服务公司
    乙方为甲方提供信息技术咨询服务、系统运维服务、
    员工技术培训服务。服务期限自 2025 年 1 月 1 日至 2025 年 12 月 31 日。
    服务费用按季度结算，每季度人民币 300,000 元。
    本技术服务协议签订后立即生效。
    """,
    "租赁合同": """
    房屋租赁合同
    甲方（出租方）：张某某
    乙方（承租方）：李四公司
    甲方将位于北京市朝阳区 XX 大厦 15 层的办公场地出租给乙方使用。
    租期为 2025 年 1 月 1 日至 2027 年 12 月 31 日，共 36 个月。
    租金为每月人民币 50,000 元整，按季支付。
    租赁协议编号：LR-2025-003
    设备租赁条款：甲方同时提供办公家具租赁。
    """,
}


# ═══════════════════════════════════════════════════════════
# 测试 1 · 规则 baseline — 无 LLM 时能正确分类 4 类合同
# ═══════════════════════════════════════════════════════════

def test_rule_classify_4_types():
    print("📋 测试 1 · 规则 baseline 分类（不依赖 LLM）")

    # 用一个永远抛异常的 mock LLM → 强制走规则
    mock_llm = _make_mock_llm_raise(Exception, "should not be called")
    clf = ContractClassifier(llm_client=mock_llm)

    expected_types = {
        "采购合同": "采购合同",
        "销售合同": "销售合同",
        "服务合同": "服务合同",
        "租赁合同": "租赁合同",
    }

    all_passed = True
    for expected_type, text in CONTRACT_TEXTS.items():
        # 规则分类是内部方法，直接调用测试
        rule_type, rule_conf = clf._rule_classify(text, expected_type)
        ok = rule_type == expected_type
        status = "✅" if ok else "❌"
        print(f"  {status} {expected_type} → 规则判断={rule_type}, 置信度={rule_conf}")
        if not ok:
            all_passed = False

    assert all_passed, "规则分类应该能正确识别全部 4 类合同"
    print("  🟢 测试 1 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 2 · LLM 高置信度 (≥0.7) → 信任 LLM 结果
# ═══════════════════════════════════════════════════════════

def test_llm_high_confidence_used():
    print("📋 测试 2 · LLM 高置信度 (≥0.7) → 信任 LLM")

    # LLM 返回高置信度采购合同
    mock_llm = _make_mock_llm('{"type": "采购合同", "confidence": 0.92}')
    clf = ContractClassifier(llm_client=mock_llm)

    text = CONTRACT_TEXTS["采购合同"]
    result = clf.classify(text, "采购合同")

    assert result.contract_type == "采购合同", f"应为采购合同，实际 {result.contract_type}"
    assert result.llm_failed == False
    assert result.method_used == "llm", f"方法应为 llm，实际 {result.method_used}"
    assert result.confidence >= 0.9, f"置信度应 ≥ 0.9，实际 {result.confidence}"
    print(f"  ✅ LLM 高置信度 (0.92) → 使用 LLM 结果 | 最终={result.contract_type} | 置信度={result.confidence} | 方法={result.method_used}")

    # 验证 llm.chat() 被调用了
    mock_llm.chat.assert_called_once()
    call_kwargs = mock_llm.chat.call_args.kwargs
    assert call_kwargs.get("json_mode") == True, f"json_mode 应为 True，实际 {call_kwargs.get('json_mode')}"
    print("  ✅ LLM 调用参数正确（json_mode=True）")

    print("  🟢 测试 2 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 3 · LLM 低置信度 (<0.7) + 与规则冲突 → ensemble 冲突融合
# ═══════════════════════════════════════════════════════════

def test_llm_low_confidence_ensemble_conflict():
    print("📋 测试 3 · LLM 低置信 + 规则冲突 → ensemble 冲突融合")

    # LLM 返回低置信度且错误判断
    mock_llm = _make_mock_llm('{"type": "其他", "confidence": 0.45}')
    clf = ContractClassifier(llm_client=mock_llm)

    text = CONTRACT_TEXTS["租赁合同"]
    result = clf.classify(text, "房屋租赁合同")

    # LLM 说 "其他"(0.45)，规则说 "租赁合同"(~0.6) → 两者不同
    # ensemble 冲突分支 → 选高置信者 rule_conf=0.6 × 0.85 = 0.51
    assert result.contract_type == "租赁合同", \
        f"ensemble 冲突应选高置信者（规则租赁合同）；期望租赁合同，实际 {result.contract_type}"
    assert result.method_used == "ensemble", f"方法应为 ensemble，实际 {result.method_used}"
    assert result.llm_failed == False
    # 冲突惩罚：base_conf(0.6) × 0.85 = 0.51
    assert abs(result.confidence - 0.51) < 0.01, \
        f"冲突惩罚后 conf 应 ≈ 0.51，实际 {result.confidence}"
    print(f"  ✅ LLM(其他,0.45) vs 规则(租赁合同,0.6) → ensemble 冲突 → 选租赁合同 conf={result.confidence}")
    print(f"  🔗 双通道详情: 规则={result.rule_result}({result.rule_confidence}), LLM={result.llm_result}({result.llm_confidence})")

    print("  🟢 测试 3 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 4 · LLM 调用异常 → 回退规则
# ═══════════════════════════════════════════════════════════

def test_llm_exception_fallback():
    print("📋 测试 4 · LLM 调用异常 → 回退规则")

    # LLM 直接抛异常（模拟网络超时）
    mock_llm = _make_mock_llm_raise(Exception, "Connection timeout")
    clf = ContractClassifier(llm_client=mock_llm)

    text = CONTRACT_TEXTS["销售合同"]
    result = clf.classify(text, "产品销售协议")

    assert result.contract_type == "销售合同", \
        f"LLM 异常应该回退到规则；期望销售合同，实际 {result.contract_type}"
    assert result.llm_failed == True, "llm_failed 应为 True"
    assert result.method_used == "rule_fallback", f"方法应为 rule_fallback，实际 {result.method_used}"
    print(f"  ✅ LLM 异常 → 回退规则 | 最终={result.contract_type} | llm_failed={result.llm_failed} | 方法={result.method_used}")

    print("  🟢 测试 4 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 5 · LLM 和规则一致 → 置信度加成
# ═══════════════════════════════════════════════════════════

def test_llm_rule_consensus_boosts_confidence():
    print("📋 测试 5 · LLM 高置信且与规则一致 → 置信度加成")

    mock_llm = _make_mock_llm('{"type": "服务合同", "confidence": 0.80}')
    clf = ContractClassifier(llm_client=mock_llm)

    text = CONTRACT_TEXTS["服务合同"]
    result = clf.classify(text, "技术服务协议")

    assert result.contract_type == "服务合同"
    assert result.method_used == "llm"
    # LLM 返回 0.80，一致时应该加成 → ≥ 0.85
    assert result.confidence >= 0.85, \
        f"LLM+规则一致时置信度应加成到 ≥ 0.85，实际 {result.confidence}"
    print(f"  ✅ LLM(0.80) + 规则一致 → 加成后置信度={result.confidence}")

    print("  🟢 测试 5 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 6 · ClassifyResult 所有字段正确填充
# ═══════════════════════════════════════════════════════════

def test_result_dataclass_fields():
    print("📋 测试 6 · ClassifyResult 字段完整性")

    mock_llm = _make_mock_llm('{"type": "租赁合同", "confidence": 0.88}')
    clf = ContractClassifier(llm_client=mock_llm)

    result = clf.classify(CONTRACT_TEXTS["租赁合同"], "场地租赁")

    # 检查所有字段
    required_fields = [
        "contract_type", "confidence",
        "rule_result", "rule_confidence",
        "llm_result", "llm_confidence",
        "llm_failed", "method_used",
    ]
    for field in required_fields:
        assert hasattr(result, field), f"ClassifyResult 缺少字段: {field}"

    # 类型检查
    assert isinstance(result.contract_type, str)
    assert isinstance(result.confidence, float)
    assert 0.0 <= result.confidence <= 1.0
    assert isinstance(result.llm_failed, bool)
    assert result.method_used in ("llm", "rule_fallback")

    print(f"  ✅ 所有 8 个字段存在且类型正确")
    print(f"     contract_type={result.contract_type}")
    print(f"     confidence={result.confidence}")
    print(f"     rule={result.rule_result}({result.rule_confidence}) | llm={result.llm_result}({result.llm_confidence})")
    print(f"     llm_failed={result.llm_failed} | method_used={result.method_used}")

    print("  🟢 测试 6 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 7 · PromptManager 集成（mock pm.render）
# ═══════════════════════════════════════════════════════════

def test_prompt_manager_integration():
    print("📋 测试 7 · PromptManager 集成")

    # mock LLM 返回成功结果
    mock_llm = _make_mock_llm('{"type": "采购合同", "confidence": 0.95}')

    # mock PromptManager
    mock_pm = MagicMock()
    mock_pm.render.return_value = {
        "name": "classify",
        "version": "v1",
        "system": "你是合同分类专家",
        "user": "请判断这个合同：\n{contract_text}\n类型列表：采购合同、销售合同、服务合同、租赁合同、其他",
    }

    clf = ContractClassifier(llm_client=mock_llm, prompt_manager=mock_pm)
    result = clf.classify("这是一个采购合同文本...", "设备采购协议")

    # 验证 pm.render 被调用（第一个参数是位置参数 "classify"）
    mock_pm.render.assert_called_once()
    call_args = mock_pm.render.call_args
    assert call_args.args[0] == "classify", f"render 第一个位置参数应为 'classify'，实际 {call_args.args[0]}"
    call_kwargs = call_args.kwargs
    assert "contract_text" in call_kwargs, "应传入 contract_text"
    assert "contract_title" in call_kwargs, "应传入 contract_title"
    print(f"  ✅ PromptManager.render('classify', contract_text=..., contract_title=...) 被正确调用")

    # 验证 LLM 收到的 messages 包含了渲染后的内容
    chat_call_args = mock_llm.chat.call_args
    messages = chat_call_args.args[0]
    assert len(messages) == 2, f"应发送 system + user 两条 messages，实际 {len(messages)}"
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    print(f"  ✅ LLM 收到正确的 messages 格式: system({messages[0]['content'][:30]}...) + user({messages[1]['content'][:30]}...)")

    # 验证 json_mode=True
    assert chat_call_args.kwargs.get("json_mode") == True
    print(f"  ✅ LLM 调用 json_mode=True")

    print(f"  最终结果: {result.contract_type} (conf={result.confidence}, method={result.method_used})")
    print("  🟢 测试 7 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 8 · 边界条件 — LLM 返回无效类型名 → 降级为其他
# ═══════════════════════════════════════════════════════════

def test_llm_invalid_type_ensemble_conflict():
    print("📋 测试 8 · LLM 返回无效类型 → 强制降置信度 → ensemble 冲突融合")

    # LLM 返回不在 VALID_TYPES 里的类型，但置信度很高
    # 旧逻辑：降级为 "其他" + 强制 conf=0.3 → 旧版纯回退规则
    # 新逻辑：降级为 "其他" + 强制 conf=0.3 → ensemble 分支（因为 0.3 < 0.7）
    #         规则说 "租赁合同"(~0.6)，LLM 说 "其他"(0.3) → 冲突 → 选高置信者规则 × 0.7
    mock_llm = _make_mock_llm('{"type": "办公合同", "confidence": 0.90}')
    clf = ContractClassifier(llm_client=mock_llm)

    text = CONTRACT_TEXTS["租赁合同"]
    result = clf.classify(text, "房屋租赁合同")

    # 关键链路：LLM 无效类型 "办公合同" → 降级为 "其他" + conf 强制 0.3
    # 0.3 < 0.7 → ensemble 分支
    # 规则 "租赁合同"(0.6) vs LLM "其他"(0.3) → 冲突 → 选 rule_conf=0.6 × 0.7 = 0.42
    assert result.contract_type == "租赁合同", \
        f"无效类型应触发 ensemble 冲突选规则；期望租赁合同，实际 {result.contract_type}"
    assert result.method_used == "ensemble", \
        f"方法应为 ensemble（LLM conf 被强制降为 0.3 < 0.7），实际 {result.method_used}"
    assert result.llm_failed == False
    assert result.llm_result == "其他"  # 降级后的类型
    assert result.llm_confidence == 0.3  # 被强制降为 0.3
    print(f"  ✅ LLM 返回无效类型 '办公合同'(conf=0.90) → 降级为 '其他' + 强制 conf=0.3")
    print(f"     → ensemble 冲突 → 选规则结果={result.contract_type}(conf={result.confidence})")

    print("  🟢 测试 8 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 9 · LLM 低置信 + 规则一致 → ensemble 共识加成
# ═══════════════════════════════════════════════════════════

def test_llm_low_confidence_ensemble_consensus():
    print("📋 测试 9 · LLM 低置信 + 规则一致 → ensemble 共识加成")

    # LLM 和规则都指向同一类型，但 LLM 置信度不高
    # 这种"双方都不确定但指向同一结论"的场景正是 ensemble 发挥价值的地方
    mock_llm = _make_mock_llm('{"type": "租赁合同", "confidence": 0.55}')
    clf = ContractClassifier(llm_client=mock_llm)

    text = CONTRACT_TEXTS["租赁合同"]
    result = clf.classify(text, "房屋租赁合同")

    # LLM "租赁合同"(0.55)，规则 "租赁合同"(~0.6) → 两者一致！
    # ensemble 共识分支：
    #   weighted = rule_conf*0.55 + llm_conf*0.45 = 0.6*0.55 + 0.55*0.45 = 0.33 + 0.2475 = 0.5775
    #   final_conf = min(0.90, 0.5775 + 0.1) = 0.6775
    assert result.contract_type == "租赁合同"
    assert result.method_used == "ensemble", f"方法应为 ensemble，实际 {result.method_used}"
    assert result.llm_failed == False
    # 共识加成后应该比任何单个通道都高
    assert result.confidence > result.rule_confidence, \
        f"共识加成后 conf({result.confidence}) 应高于 rule_confidence({result.rule_confidence})"
    assert result.confidence > result.llm_confidence, \
        f"共识加成后 conf({result.confidence}) 应高于 llm_confidence({result.llm_confidence})"

    # 具体数值验证（假设规则 conf ≈ 0.6）
    # weighted = 0.6*0.55 + 0.55*0.45 = 0.5775 → +0.1 = 0.6775
    expected_approx = 0.678
    assert abs(result.confidence - expected_approx) < 0.02, \
        f"共识加成后 conf 应 ≈ {expected_approx}，实际 {result.confidence}"

    print(f"  ✅ LLM(租赁合同,0.55) + 规则(租赁合同,{result.rule_confidence})")
    print(f"     → ensemble 共识 → conf={result.confidence}（> 单通道的 {result.rule_confidence}/{result.llm_confidence}）")

    print("  🟢 测试 9 通过\n")


# ═══════════════════════════════════════════════════════════
# 运行全部测试
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("M11 · 合同分类模块 — 本地 mock 测试")
    print("=" * 60 + "\n")

    test_rule_classify_4_types()
    test_llm_high_confidence_used()
    test_llm_low_confidence_ensemble_conflict()
    test_llm_exception_fallback()
    test_llm_rule_consensus_boosts_confidence()
    test_result_dataclass_fields()
    test_prompt_manager_integration()
    test_llm_invalid_type_ensemble_conflict()
    test_llm_low_confidence_ensemble_consensus()

    print("=" * 60)
    print("🎉 全部 9 项测试通过 ✅")
    print("=" * 60)

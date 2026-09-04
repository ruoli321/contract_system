"""M10 · LLM 客户端封装 — 本地 mock 测试（不调真实 API）"""
import sys, os, json, time
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from app.services.llm_client import (
    BaseLLM, OpenAILLM, DeepSeekLLM, create_llm, LLMResponse,
)

# ── 构造 openai SDK 风格的 Mock Response ──
def _make_openai_response(content: str, model="deepseek-chat"):
    """模拟 openai.chat.completions.create() 的返回结构"""
    choice = MagicMock()
    choice.message.content = content
    choice.finish_reason = "stop"

    resp = MagicMock()
    resp.choices = [choice]
    resp.model = model
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {"prompt_tokens": 10, "completion_tokens": 5}
    return resp


# ════════════════════════════════════════════════════
# 测试 1 · 工厂方法 + 子类实例化
# ════════════════════════════════════════════════════
def test_factory_and_subclasses():
    print("📋 测试 1 · 工厂 + 子类实例化")

    # 1a. DeepSeek
    ds = create_llm("deepseek", api_key="sk-test")
    assert isinstance(ds, DeepSeekLLM), f"应为 DeepSeekLLM，实际 {type(ds)}"
    assert ds.base_url == "https://api.deepseek.com/v1"
    assert ds.model == "deepseek-chat"
    print("  ✅ DeepSeekLLM 正确创建")

    # 1b. OpenAI
    oa = create_llm("openai", api_key="sk-test", model="gpt-4o")
    assert isinstance(oa, OpenAILLM)
    assert oa.base_url == "https://api.openai.com/v1"
    assert oa.model == "gpt-4o"
    print("  ✅ OpenAILLM 正确创建")

    # 1c. qwen (DashScope)
    qw = create_llm("qwen", api_key="sk-test")
    assert isinstance(qw, OpenAILLM)  # OpenAI 兼容
    assert "dashscope" in qw.base_url
    print("  ✅ qwen/DashScope 走 OpenAILLM + 自定义 base_url")

    # 1d. 未知 provider 抛异常
    try:
        create_llm("unknown", api_key="sk-test")
        assert False, "应该抛 ValueError"
    except ValueError as e:
        print(f"  ✅ 未知 provider 正确抛异常: {e}")

    print("  🟢 测试 1 通过\n")


# ════════════════════════════════════════════════════
# 测试 2 · chat() 基本调用 + LLMResponse
# ════════════════════════════════════════════════════
def test_chat_basic():
    print("📋 测试 2 · chat() 基本调用")

    llm = DeepSeekLLM(api_key="sk-test", max_retries=0)

    with patch.object(llm, "_ensure_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response("Hello World!")

        resp = llm.chat([
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hi"},
        ], temperature=0.1)

        assert isinstance(resp, LLMResponse)
        assert resp.content == "Hello World!"
        assert resp.model == "deepseek-chat"
        assert resp.usage == {"prompt_tokens": 10, "completion_tokens": 5}
        print(f"  ✅ chat() 返回 LLMResponse 正确: content={resp.content!r}, model={resp.model}")

        # 验证 API 调用参数
        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("temperature") == 0.1
        assert call_kwargs.kwargs.get("response_format") is None  # 非 json_mode
        print("  ✅ API 调用参数正确（temperature / 非 json_mode）")

    print("  🟢 测试 2 通过\n")


# ════════════════════════════════════════════════════
# 测试 3 · json_mode=True → response_format={"type":"json_object"}
# ════════════════════════════════════════════════════
def test_json_mode():
    print("📋 测试 3 · json_mode=True")

    llm = DeepSeekLLM(api_key="sk-test", max_retries=0)

    with patch.object(llm, "_ensure_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            '{"type": "采购合同", "confidence": 0.95}'
        )

        resp = llm.chat(
            [{"role": "user", "content": "分类这个合同"}],
            json_mode=True,
        )

        # 验证 response_format 被设置
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("response_format") == {"type": "json_object"}, \
            f"response_format 应为 {{'type':'json_object'}}，实际 {call_kwargs.get('response_format')}"
        print("  ✅ json_mode=True → response_format={'type':'json_object'}")

        # 返回的 content 应该被 json.loads 成功（我们没自己解析，只验证了 response）
        data = json.loads(resp.content)
        assert data["type"] == "采购合同"
        print(f"  ✅ 返回 JSON 可正常解析: {data}")

    print("  🟢 测试 3 通过\n")


# ════════════════════════════════════════════════════
# 测试 4 · json_mode 且返回的不是有效 JSON → 触发重试
# ════════════════════════════════════════════════════
def test_json_mode_retry_on_bad_json():
    print("📋 测试 4 · json_mode 下 JSON 无效 → 重试")

    llm = DeepSeekLLM(api_key="sk-test", max_retries=2, base_delay=0.01)

    with patch.object(llm, "_ensure_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        # 第一次返回坏 JSON，第二次返回好 JSON
        mock_client.chat.completions.create.side_effect = [
            _make_openai_response('{bad json...'),        # 触发 JSONDecodeError
            _make_openai_response('{"ok": true}'),
        ]

        resp = llm.chat([{"role": "user", "content": "go"}], json_mode=True)

        assert json.loads(resp.content) == {"ok": True}
        # create 应该被调用了 2 次
        assert mock_client.chat.completions.create.call_count == 2, \
            f"应该调用 2 次（1 次失败 + 1 次成功），实际 {mock_client.chat.completions.create.call_count}"
        print(f"  ✅ JSON 无效时自动重试，共调用 {mock_client.chat.completions.create.call_count} 次")

    print("  🟢 测试 4 通过\n")


# ════════════════════════════════════════════════════
# 测试 5 · 网络异常 → 指数退避重试
# ════════════════════════════════════════════════════
def test_network_error_retry():
    print("📋 测试 5 · 网络异常 → 指数退避")

    import openai as openai_sdk

    llm = DeepSeekLLM(api_key="sk-test", max_retries=3, base_delay=0.01)

    with patch.object(llm, "_ensure_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        # 连续 3 次 APITimeoutError，第 4 次成功
        mock_client.chat.completions.create.side_effect = [
            openai_sdk.APITimeoutError(request=MagicMock()),
            openai_sdk.APITimeoutError(request=MagicMock()),
            openai_sdk.APITimeoutError(request=MagicMock()),
            _make_openai_response("final success"),
        ]

        resp = llm.chat([{"role": "user", "content": "go"}])
        assert resp.content == "final success"
        assert mock_client.chat.completions.create.call_count == 4  # 3 次重试 + 1 次成功
        print(f"  ✅ 3 次 APITimeoutError 后成功，共调用 {mock_client.chat.completions.create.call_count} 次")

    print("  🟢 测试 5 通过\n")


# ════════════════════════════════════════════════════
# 测试 6 · invoke() 兼容接口（str / list 两种格式）
#   classifier.py → invoke("prompt 字符串")
#   extractor.py → invoke([{role:system}, {role:user}])
# ════════════════════════════════════════════════════
def test_invoke_compat():
    print("📋 测试 6 · invoke() 兼容接口")

    llm = DeepSeekLLM(api_key="sk-test", max_retries=0)

    with patch.object(llm, "_ensure_client") as mock_client_fn:
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            '{"type": "采购合同"}'
        )

        # 6a. classifier 用的：invoke(str)
        resp1 = llm.invoke("判断合同类型：...")
        assert isinstance(resp1, LLMResponse)
        assert hasattr(resp1, "content")
        assert resp1.content == '{"type": "采购合同"}'
        print(f"  ✅ invoke(str) 兼容 classifier.py")

        # 6b. extractor 用的：invoke(list[dict])
        resp2 = llm.invoke([
            {"role": "system", "content": "你是专家"},
            {"role": "user", "content": "提取字段"},
        ])
        assert isinstance(resp2, LLMResponse)
        assert resp2.content == '{"type": "采购合同"}'
        print(f"  ✅ invoke(list[dict]) 兼容 extractor.py")

        # 6c. 验证内部走了正确的 messages 格式
        call_kwargs = mock_client.chat.completions.create.call_args_list
        # 第一次调用应该收到 user message
        first_call_msgs = call_kwargs[0].kwargs["messages"]
        assert len(first_call_msgs) == 1 and first_call_msgs[0]["role"] == "user", \
            f"invoke(str) 应包装成 [{{role:user}}]，实际 {first_call_msgs}"
        print(f"  ✅ invoke(str) 自动包装成 user message")

    print("  🟢 测试 6 通过\n")


# ════════════════════════════════════════════════════
# 测试 7 · 未知 provider → factory 抛 ValueError
# ════════════════════════════════════════════════════
def test_unknown_provider():
    print("📋 测试 7 · 边界条件")
    try:
        create_llm("bogus_provider", api_key="x")
        print("  ❌ 未抛异常")
    except ValueError as e:
        print(f"  ✅ 正确拒绝未知 provider")

    # invoke 错误类型
    llm = DeepSeekLLM(api_key="sk-test", max_retries=0)
    try:
        llm.invoke(12345)  # 错误类型
        print("  ❌ invoke(12345) 未抛异常")
    except TypeError as e:
        print(f"  ✅ invoke(错误类型) 正确抛 TypeError: {e}")

    print("  🟢 测试 7 通过\n")


# ════════════════════════════════════════════════════
# 运行全部测试
# ════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("M10 · LLM 客户端封装 — 本地 mock 测试")
    print("=" * 60 + "\n")

    test_factory_and_subclasses()
    test_chat_basic()
    test_json_mode()
    test_json_mode_retry_on_bad_json()
    test_network_error_retry()
    test_invoke_compat()
    test_unknown_provider()

    print("=" * 60)
    print("🎉 全部 7 项测试通过 ✅")
    print("=" * 60)

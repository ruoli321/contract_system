# ═══════════════════════════════════════════════════════════════════
# M10 · LLM 客户端封装（抽象基类 + Provider 子类 + 工厂）
#
# 设计要点：
#   - 用 raw openai SDK（OpenAILLM / DeepSeekLLM 只是 base_url 不同）
#   - BaseLLM 定义 chat(messages, temperature, json_mode) 接口
#   - 额外提供 invoke(prompt_or_messages) → 返回有 .content 的对象
#     （兼容 classifier.py / extractor.py 现有代码，无需修改）
#   - json_mode=True → response_format={"type":"json_object"}
#   - 指数退避重试：openai.APIError / APITimeoutError / JSONDecodeError
#   - 工厂函数 create_llm(provider, settings) 一键创建
# ═══════════════════════════════════════════════════════════════════
import abc
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import openai

logger = logging.getLogger("llm-client")


# ═════════════════════════════════════════════════════════════
# 响应包装：模拟 LangChain AIMessage 的 .content 接口
# ═════════════════════════════════════════════════════════════

@dataclass
class LLMResponse:
    """LLM 响应对象，兼容 LangChain AIMessage 的 .content 访问方式"""
    content: str
    raw: Any = None                # openai 返回的原始 response
    usage: Optional[dict] = None   # token 用量统计
    model: str = ""                # 使用的模型名
    finish_reason: str = ""        # "stop" / "length" / "tool_calls" 等

    def __str__(self) -> str:
        return self.content


# ═════════════════════════════════════════════════════════════
# BaseLLM · 抽象基类
# ═════════════════════════════════════════════════════════════

class BaseLLM(abc.ABC):
    """LLM 客户端抽象基类"""

    # 默认超时（秒）和重试配置
    DEFAULT_TIMEOUT = 60.0
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_BASE_DELAY = 1.0   # 第一次重试等 1s，第二次 2s，第三次 4s

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_delay = base_delay

        # 子类在 __init__ 中创建实际的 openai 客户端
        self._client: Optional[openai.OpenAI] = None

    # ── 必须由子类实现 ──
    @abc.abstractmethod
    def _create_client(self) -> openai.OpenAI:
        """创建 openai.OpenAI 实例（子类实现不同 provider 的默认 base_url / headers）"""
        ...

    # ── 公开 API ──

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        """
        核心接口：发送 messages 给 LLM，返回 LLMResponse

        Args:
            messages:   [{"role":"system","content":"..."}, {"role":"user","content":"..."}]
            temperature: 0.0 = 确定性（推荐提取/分类任务）
            json_mode:   True 时用 response_format={"type":"json_object"} 强制 JSON 输出
            max_tokens:  可选，限制最大输出 token
            **kwargs:    透传给 chat.completions.create（如 top_p, stop 等）

        Returns:
            LLMResponse(content=..., raw=..., usage=..., model=...)
        """
        client = self._ensure_client()
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=self.timeout,
                    response_format={"type": "json_object"} if json_mode else None,
                    **kwargs,
                )

                # 提取文本
                choice = resp.choices[0]
                content = choice.message.content or ""
                finish_reason = choice.finish_reason or ""

                # 如果要求 JSON 模式，验证返回的真的是 JSON
                if json_mode and content.strip():
                    try:
                        json.loads(content)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            f"JSON 解析失败 (attempt {attempt+1}/{self.max_retries+1})，"
                            f"将重试 | 错误: {e}"
                        )
                        raise  # 抛出 → 进入重试逻辑

                return LLMResponse(
                    content=content,
                    raw=resp,
                    usage=resp.usage.model_dump() if resp.usage else None,
                    model=resp.model or self.model,
                    finish_reason=finish_reason,
                )

            except (openai.APIError, openai.APITimeoutError,
                    openai.APIConnectionError, openai.RateLimitError,
                    json.JSONDecodeError) as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = self.base_delay * (2 ** attempt)  # 指数退避: 1s, 2s, 4s
                    logger.warning(
                        f"LLM 调用失败 (attempt {attempt+1}/{self.max_retries+1}): {e} | "
                        f"等待 {delay:.1f}s 后重试"
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"LLM 调用耗尽重试次数 ({self.max_retries+1} 次) 仍失败: {e}")

        # 所有重试都失败
        raise RuntimeError(
            f"LLM 调用失败（provider={self.__class__.__name__}, model={self.model}, "
            f"重试 {self.max_retries+1} 次）: {last_error}"
        ) from last_error

    # ── 兼容现有 classifier/extractor 代码 ──

    def invoke(self, prompt_or_messages) -> LLMResponse:
        """
        LangChain 兼容接口：接收 str 或 list[dict]，返回有 .content 的 LLMResponse。

        classifier.py 用: response = self.llm.invoke(prompt_string)
        extractor.py 用:  response = self.llm.invoke([{role:system,...}, {role:user,...}])
        两者都检查:        content = response.content if hasattr(response, "content") else str(response)

        → 直接返回 LLMResponse，它有 .content 属性 ✅
        """
        if isinstance(prompt_or_messages, str):
            # 单字符串 → 包装成 user message
            messages = [{"role": "user", "content": prompt_or_messages}]
        elif isinstance(prompt_or_messages, list):
            messages = prompt_or_messages
        else:
            raise TypeError(f"invoke() 只接受 str 或 list[dict]，收到 {type(prompt_or_messages)}")

        # 默认不强制 json_mode（让 classifier/extractor 自己解析）
        return self.chat(messages, temperature=0.1, json_mode=False)

    # ── 内部 ──

    def _ensure_client(self) -> openai.OpenAI:
        """延迟创建 openai client（避免无 API Key 时 import 就崩）"""
        if self._client is None:
            self._client = self._create_client()
            logger.info(
                f"✅ {self.__class__.__name__} 初始化 | "
                f"base_url={self.base_url} | model={self.model} | timeout={self.timeout}s"
            )
        return self._client

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(model={self.model}, "
            f"base_url={self.base_url}, retries={self.max_retries})"
        )


# ═════════════════════════════════════════════════════════════
# OpenAILLM · OpenAI 官方 endpoint
# ═════════════════════════════════════════════════════════════

class OpenAILLM(BaseLLM):
    """OpenAI 官方 Chat Completions（或 OpenAI 兼容 endpoint）"""

    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url or self.DEFAULT_BASE_URL,
            **kwargs,
        )

    def _create_client(self) -> openai.OpenAI:
        return openai.OpenAI(api_key=self.api_key, base_url=self.base_url)


# ═════════════════════════════════════════════════════════════
# DeepSeekLLM · DeepSeek endpoint（OpenAI 兼容）
# ═════════════════════════════════════════════════════════════

class DeepSeekLLM(BaseLLM):
    """DeepSeek 模型（完全 OpenAI 兼容，只是 base_url 不同）"""

    DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url or self.DEFAULT_BASE_URL,
            **kwargs,
        )

    def _create_client(self) -> openai.OpenAI:
        return openai.OpenAI(api_key=self.api_key, base_url=self.base_url)


# ═════════════════════════════════════════════════════════════
# 工厂函数 · create_llm()
# ═════════════════════════════════════════════════════════════

def create_llm(
    provider: str,
    api_key: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    **kwargs,
) -> BaseLLM:
    """
    工厂方法：根据 provider 创建对应 LLM 客户端

    Args:
        provider:  "deepseek" / "openai" / "qwen" / "anthropic"
                  （qwen/anthropic 也是 OpenAI 兼容，走 OpenAILLM + 自定义 base_url）
        api_key:   API Key
        model:     模型名（None 用 provider 默认值）
        base_url:  自定义 endpoint（None 用 provider 默认值）
        **kwargs:  透传给 BaseLLM（timeout, max_retries, base_delay）

    Returns:
        OpenAILLM 或 DeepSeekLLM 实例
    """
    provider = provider.lower().strip()

    # DeepSeek 用自己的类（默认 base_url 不同）
    if provider == "deepseek":
        return DeepSeekLLM(api_key=api_key, model=model or DeepSeekLLM.DEFAULT_MODEL,
                           base_url=base_url, **kwargs)

    # 其他所有 provider 都是 OpenAI 兼容 → OpenAILLM + 自定义 base_url
    if provider in ("openai", "qwen", "dashscope", "anthropic", "custom"):
        # qwen (DashScope) 默认 base_url
        if provider in ("qwen", "dashscope") and not base_url:
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            model = model or "qwen-plus"
        return OpenAILLM(api_key=api_key, model=model or "gpt-4o-mini",
                         base_url=base_url, **kwargs)

    raise ValueError(
        f"未知 provider: {provider}。支持: deepseek / openai / qwen / anthropic"
    )

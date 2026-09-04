# ═══════════════════════════════════════════
# AI 服务配置管理（pydantic-settings，支持 .env）
# ═══════════════════════════════════════════
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    """全局配置，从 .env 或环境变量加载"""

    # ── LLM 配置 ──
    llm_provider: str = Field(default="deepseek", description="deepseek | qwen | anthropic | openai（都是 OpenAI 兼容）")
    llm_api_key: str = Field(default="", description="LLM API Key")
    llm_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="OpenAI 兼容的 Base URL"
    )
    llm_model: str = Field(default="qwen-plus", description="LLM 模型名")

    # ── Embedding（默认 BAAI/bge-small-zh-v1.5，512 维，加载快）──
    embedding_model: str = Field(default="BAAI/bge-small-zh-v1.5", description="Embedding 模型（完整 HuggingFace 名）")
    embedding_dimension: int = Field(default=512, description="向量维度（bge-small 是 512，bge-base 是 768，bge-large 是 1024）")

    # ── Chroma 向量库（独立服务，通过 HTTP 访问）──
    # 容器内默认 host=chroma, port=8000；本地开发改 host=localhost, port=8001
    chroma_host: str = Field(default="chroma", description="Chroma 服务地址")
    chroma_port: int = Field(default=8000, description="Chroma 服务端口（容器内部 8000，宿主机映射 8001）")
    chroma_collection_examples: str = Field(default="contract_examples", description="标准范例集合名")
    chroma_collection_chunks: str = Field(default="contract_chunks", description="当前合同切块集合名")

    # ── 标准合同范例数据 ──
    annotations_dir: str = Field(default="./examples/annotations", description="合同标注 JSON 目录")
    test_pdfs_dir: str = Field(default="", description="合同原文 txt 目录（容器内挂载路径，如 /test_pdfs）")

    # ── Prompt 管理 ──
    prompts_dir: str = Field(default="./prompts", description="提示词模板目录")
    current_prompt_version: str = Field(default="v1", description="当前提示词版本")

    # ── 服务 ──
    log_level: str = Field(default="INFO", description="日志级别")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    """单例配置（用 lru_cache 避免重复加载 .env）"""
    return Settings()

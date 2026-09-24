# ═══════════════════════════════════════════════════════════════════
# M13 · 合同字段提取模块（核心得分模块）
#
# 依赖：M9 PromptManager（YAML render）、M10 BaseLLM.chat + json_mode
#       M12 RAGLearner（retrieve_examples + format_few_shot）
#       M20 postprocess_fallback（正则补缺）
#       M21 validator（统一校验）+ gatekeeper（上游生成 quality 块）
#
# 提取流程（extract_contract_fields，M21 管线重排）：
#   1. 加载 M9 提示词模板：system_prompt（复用 system_extract）+ user_template（extract）
#   2. 若 use_rag=True → RAGLearner 检索 3 个相似范例 → format_few_shot 拼进 user prompt
#   3. 调用 llm.chat(messages, json_mode=True, temperature=0.0)
#   4. 解析 JSON + Pydantic ContractExtraction 校验
#   5. 正则兜底补缺（postprocess_fill，只补缺失不覆盖）
#   6. 统一校验器 validate_extraction：证据比对 / 金额交叉 / sanity / 枚举
#      → 硬错误全量喂回重试（extract_retry 模板，最多 3 次 + 时间预算熔断）
#      → fatal/warnings 不重试，随结果返回交 gatekeeper 转人工
#   7. 返回 ExtractResult（fields + 校验报告 + 结构化日志）
#
# 字段命名约定（与 field_dict.json 保持一致）：
#   partner_a / partner_b / amount_uppercase / sign_date / effective_date / expire_date
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, create_model

from .postprocess_fallback import fill as postprocess_fill
from .validator import validate_extraction, LLM_TIME_BUDGET

logger = logging.getLogger("extractor")


# ═══════════════════════════════════════════════════════════════════
# Schema 动态构建（单一数据源：field_dict.json extract_schema.fields）
# ═══════════════════════════════════════════════════════════════════

def _resolve_enum_refs(values, field_dict: dict) -> Optional[list]:
    """
    解析枚举值引用。
    支持：
      - list → 直接返回
      - "contract_type.values" → 点号路径 → field_dict["contract_type"]["values"]
    找不到返回 None（字段会退化为普通 str）。
    """
    if isinstance(values, list):
        return values
    if isinstance(values, str) and "." in values:
        parts = values.split(".")
        obj: Any = field_dict
        for p in parts:
            if isinstance(obj, dict) and p in obj:
                obj = obj[p]
            else:
                return None
        if isinstance(obj, list):
            return obj
    return None


def build_extraction_model(
    fields: dict,
    field_dict: dict,
    model_name: str = "ContractExtraction",
) -> type[BaseModel]:
    """
    从 field_dict 的 schema 定义动态构建 Pydantic model。

    类型映射（运行时校验）：
      string/null  → Optional[str], default=None
      number|null  → Optional[float], default=None
      string       → str, ... (required)
      number       → float, ... (required)
      enum         → Literal[v1, v2, ...]（真正的运行时约束！）

    单一数据源：field_dict.extract_schema.fields
    PromptManager.format_schema_text() 和这里用同一份 fields dict，
    保证 prompt 里的 Schema 描述和 Pydantic 校验规则始终一致。
    """
    type_map = {
        "string": str,
        "number": float,
        "integer": int,
        "boolean": bool,
        "string|null": Optional[str],
        "number|null": Optional[float],
        "integer|null": Optional[int],
        "boolean|null": Optional[bool],
    }

    field_defs: dict[str, Any] = {}

    for fname, fdef in fields.items():
        ftype = fdef.get("type", "string")
        required = fdef.get("required", False)
        desc = fdef.get("desc", fname)

        if ftype == "enum":
            # enum → Literal[v1, v2, ...]（真正运行时校验，旧版 Field(enum=) 无效）
            enum_values = _resolve_enum_refs(fdef.get("values", []), field_dict) or []
            literal_type = Literal[tuple(enum_values)] if enum_values else str  # type: ignore[valid-type]
            if required:
                field_defs[fname] = (literal_type, Field(..., description=desc))
            else:
                field_defs[fname] = (Optional[literal_type], Field(None, description=desc))
        elif ftype in type_map:
            py_type = type_map[ftype]
            default = ... if required else None
            field_defs[fname] = (py_type, Field(default, description=desc))
        else:
            # 未知类型 → fallback 为 str
            py_type = str if required else Optional[str]
            default = ... if required else None
            field_defs[fname] = (py_type, Field(default, description=desc))

    return create_model(model_name, **field_defs)


def _load_extract_schema_from_disk() -> tuple[dict, dict]:
    """
    从 field_dict.json 磁盘文件加载 extract_schema。
    用于模块级 fallback（test 直接 import ContractExtraction 时）。
    查找顺序：
      1. 当前工作目录/prompts/field_dict.json
      2. __file__ ../../../../prompts/field_dict.json（从 app/services/extractor.py 反推）
    """
    candidates = [
        Path.cwd() / "prompts" / "field_dict.json",
        Path(__file__).resolve().parent.parent.parent.parent / "prompts" / "field_dict.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                schema = data.get("extract_schema", {})
                return schema.get("fields", {}), data
            except Exception as e:
                logger.debug(f"读取 field_dict.json 失败 [{path}]: {e}")
    logger.warning("field_dict.json 未找到，ContractExtraction 将为空 Schema")
    return {}, {}


# ── 模块级 fallback model（供 test import + 无 PromptManager 场景使用）──
_FALLBACK_FIELDS, _FALLBACK_FIELD_DICT = _load_extract_schema_from_disk()
ContractExtraction = build_extraction_model(_FALLBACK_FIELDS, _FALLBACK_FIELD_DICT) if _FALLBACK_FIELDS else BaseModel
logger.info(f"📦 ContractExtraction 动态构建 | 字段数: {len(_FALLBACK_FIELDS)}")


# ═══════════════════════════════════════════════════════════════════
# 返回结果（结构化日志 + 提取字段分离）
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ExtractResult:
    """extract_contract_fields 的完整返回值"""
    # 提取字段（核心）
    fields: dict

    # 结构化日志（要求 #6）
    prompt_version: str                   # 使用的 extract 提示词版本
    system_prompt_name: str               # system prompt 来源名
    few_shot_sources: list[str] = field(default_factory=list)  # 使用的范例 id 列表
    few_shot_count: int = 0               # 使用了几个范例
    used_rag: bool = False                # 是否启用 RAG
    attempt_count: int = 1                # 实际尝试次数（含重试）
    elapsed_seconds: float = 0.0         # 总耗时
    validation_errors: list[str] = field(default_factory=list) # 重试后仍未解决的硬错误（空=通过）
    llm_failed: bool = False              # LLM 调用是否异常

    # ── M21 统一校验器输出（gatekeeper 生成 quality 块的数据源）──
    fatal_errors: list[str] = field(default_factory=list)      # 不可重试错误（原文缺失/必填缺）
    warnings: list[str] = field(default_factory=list)          # 警告（转 needs_review）
    critical_missing: list[str] = field(default_factory=list)  # 缺失的 critical 字段
    field_evidence: dict = field(default_factory=dict)         # 字段证据比对结果
    system_confidence: float = 0.0                             # 系统置信度（替代 LLM 自报）
    is_fallback: bool = False                                  # 是否为 LLM 全败后的正则抢救结果

    # 便捷属性
    @property
    def confidence(self) -> float:
        return self.fields.get("confidence", 0.0)

    @property
    def contract_type(self) -> str:
        return self.fields.get("contract_type", "其他")

    def to_api_dict(self) -> dict:
        """给 main.py API 用的精简返回

        ⚠️ 拍平 self.fields，不再额外包一层 "extraction"：
           self.fields = {contract_name, amount, ..., confidence, contract_type}
           直接返回这些字段 + 元数据，避免 data.extraction.extraction.xxx 双层嵌套
        """
        result = dict(self.fields)  # 浅拷贝，直接拿字段
        # 追加元数据（不覆盖 fields 里已有的 key）
        result.setdefault("used_few_shot", self.used_rag)
        result.setdefault("attempt_count", self.attempt_count)
        result.setdefault("elapsed_seconds", round(self.elapsed_seconds, 3))
        # M21：confidence 统一为系统置信度（LLM 自报值不可信，见 validator）
        result["confidence"] = self.system_confidence or result.get("confidence", 0.0)
        return result


# ═══════════════════════════════════════════════════════════════════
# ContractExtractor · 主类
# ═══════════════════════════════════════════════════════════════════

class ContractExtractor:
    """
    合同字段提取器 — RAG + LLM + JSON Schema 校验 + 重试

    注入依赖（低耦合）：
        llm_client:      M10 BaseLLM 实例（有 .chat() 方法）
        prompt_manager:  M9 PromptManager 实例（有 .render() 方法）
        rag_learner:      M12 RAGLearner 实例（有 .retrieve_examples() 方法，可选）

    不直接依赖 VectorStore、Chroma、PDFParser —— 全通过注入接口。
    """

    # 重试上限（要求 #5 最多 3 次）
    MAX_RETRIES = 3

    # 合同文本截断上限（防超 token）
    MAX_TEXT_CHARS = 8000

    # Prompt 名称（M9 prompts.yaml 里已定义）
    SYSTEM_PROMPT_NAME = "system_extract"
    USER_PROMPT_NAME = "extract"
    RETRY_PROMPT_NAME = "extract_retry"

    def __init__(
        self,
        llm_client,
        prompt_manager,
        rag_learner: Optional[object] = None,
    ):
        self.llm = llm_client
        self.pm = prompt_manager
        self.rag = rag_learner  # Optional: None 时禁用 RAG

        # ── 动态构建 Pydantic model（单一数据源：field_dict.json）──
        # PromptManager.field_dict 如果是真实 dict → 用它重建
        # 如果是 mock（MagicMock）→ 用模块级 fallback
        self._extraction_model = ContractExtraction  # 默认 fallback
        if (
            hasattr(prompt_manager, "field_dict")
            and isinstance(prompt_manager.field_dict, dict)
        ):
            try:
                schema_fields = prompt_manager.field_dict.get("extract_schema", {}).get("fields", {})
                if schema_fields:
                    self._extraction_model = build_extraction_model(
                        schema_fields, prompt_manager.field_dict
                    )
                    logger.info(f"  ✅ Extraction model 从 pm.field_dict 动态构建（{len(schema_fields)} 字段）")
            except Exception as e:
                logger.warning(f"  ⚠️  动态构建 model 失败，用 fallback: {e}")

        logger.info(
            f"📋 ContractExtractor 初始化 | "
            f"llm={type(llm_client).__name__} | "
            f"prompt_manager={type(prompt_manager).__name__} | "
            f"rag_learner={'✅' if rag_learner else '❌'}"
        )

    # ═══════════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════════

    def extract_contract_fields(
        self,
        contract_text: str,
        contract_type: str = None,
        use_rag: bool = True,
        top_k_examples: int = 3,
        exclude_ids: list = None,
    ) -> ExtractResult:
        """
        合同文本 → 结构化提取结果（含日志）

        Args:
            contract_text:     待提取的完整合同文本
            contract_type:     已判断的合同类型（分类模块输出），可空（空则用 "其他"）
            use_rag:           是否启用 RAG few-shot（要求 #2）
            top_k_examples:    检索相似范例数量
            exclude_ids:       可选，leave-one-out 用的排除列表（如评估时排除自身）

        Returns:
            ExtractResult（dataclass，含 fields + 结构化日志）
        """
        t0 = time.time()
        ctype = contract_type or "其他"
        truncated_text = contract_text[:self.MAX_TEXT_CHARS]

        # ── 准备 RAG few-shot ──
        few_shot_text = "（暂无参考范例）"
        few_shot_sources = []
        rag_used = False

        if use_rag and self.rag is not None:
            try:
                # 第一次：按 contract_type 精准过滤
                few_shot_text, raw_examples = self.rag.retrieve_and_format_few_shot(
                    query_text=truncated_text,
                    contract_type=ctype,
                    top_k=top_k_examples,
                    exclude_ids=exclude_ids,
                )

                # fallback 策略分层：
                #   1. 结果 == 0 → 完全降级跨类型
                #   2. 0 < 结果 < top_k → 跨类型补齐到 top_k
                if not raw_examples and ctype:
                    logger.info(
                        f"  📌 按类型 '{ctype}' 过滤无匹配 → 降级为跨类型检索"
                    )
                    few_shot_text, raw_examples = self.rag.retrieve_and_format_few_shot(
                        query_text=truncated_text,
                        contract_type=None,
                        top_k=top_k_examples,
                        exclude_ids=exclude_ids,
                    )
                elif 0 < len(raw_examples) < top_k_examples and ctype:
                    logger.info(
                        f"  📌 按类型 '{ctype}' 只拿到 {len(raw_examples)} 个 → "
                        f"跨类型补齐到 top_k={top_k_examples}"
                    )
                    extras_str, extras = self.rag.retrieve_and_format_few_shot(
                        query_text=truncated_text,
                        contract_type=None,
                        top_k=top_k_examples,
                        exclude_ids=exclude_ids,
                    )
                    # 合并：先加类型匹配的，再补跨类型去重
                    existing_ids = {e.get("id") for e in raw_examples}
                    for ex in extras:
                        if ex.get("id") not in existing_ids and len(raw_examples) < top_k_examples:
                            raw_examples.append(ex)
                            existing_ids.add(ex.get("id"))
                    few_shot_text = self.rag.format_few_shot(raw_examples)

                few_shot_sources = [ex.get("id", "?") for ex in raw_examples]
                rag_used = len(raw_examples) > 0
                logger.info(
                    f"  🔍 RAG: 检索到 {len(raw_examples)} 个范例 | "
                    f"sources={few_shot_sources}"
                )
            except Exception as e:
                logger.warning(f"  ⚠️  RAG 检索失败，降级为无范例: {e}")
                few_shot_text = "（暂无参考范例）"

        elif use_rag and self.rag is None:
            logger.warning("  ⚠️  use_rag=True 但 RAGLearner 未注入，跳过 RAG")

        # ── 准备 System Prompt（复用 system_extract，Schema 从 field_dict.json 动态生成）──
        # pm.render() 返回 {"system": str, "user": str, "name": str, "version": str}
        # 单一数据源：field_dict.json → extract_schema.fields → format_schema_text()
        json_schema_text = self.pm.format_schema_text("extract_schema")
        system_render = self.pm.render(self.SYSTEM_PROMPT_NAME, json_schema=json_schema_text)
        system_prompt = system_render["system"]
        system_prompt_version = system_render["version"]

        # ── 主循环：最多 3 次尝试（次数 + 时间双重熔断，M21）──
        last_error = None
        last_validation_errors: list[str] = []
        last_report = None                # 最后一次校验报告（耗尽时随成果返回）
        last_merged: Optional[dict] = None  # 最后一次解析成功的字段（耗尽时返回）

        for attempt in range(1, self.MAX_RETRIES + 1):
            # 时间熔断：至少保证 1 次尝试，后续尝试前检查预算
            if attempt > 1 and (time.time() - t0) > LLM_TIME_BUDGET:
                logger.warning(f"  ⏱️ LLM 时间预算耗尽（>{LLM_TIME_BUDGET:.0f}s），停止重试 → 转人工")
                break
            logger.info(f"  🔄 提取尝试 {attempt}/{self.MAX_RETRIES}")

            try:
                if attempt == 1:
                    # 首次：用 extract 模板
                    user_render = self.pm.render(
                        self.USER_PROMPT_NAME,
                        contract_type=ctype,
                        contract_text=truncated_text,
                        few_shot_examples=few_shot_text,
                    )
                    user_prompt = user_render["user"]
                else:
                    # 重试：用 extract_retry 模板（把上次的错误全量喂回 LLM 自我纠正）
                    retry_render = self.pm.render(
                        self.RETRY_PROMPT_NAME,
                        contract_text=truncated_text,
                        previous_result=json.dumps(last_error or {}, ensure_ascii=False, indent=2),
                        validation_errors="\n".join(f"  · {e}" for e in last_validation_errors) or "（无具体错误信息）",
                    )
                    user_prompt = retry_render["user"]

                # 调用 LLM（M10 API：chat + json_mode）
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
                t_llm = time.time()
                raw_response = self.llm.chat(messages, temperature=0.0, json_mode=True)
                llm_elapsed = time.time() - t_llm
                logger.info(f"    ⏱️ LLM 调用耗时 {llm_elapsed:.2f}s")

                # ── DEBUG: 打印 LLM 原始返回类型和前 500 字 ──
                try:
                    raw_type = type(raw_response).__name__
                    if hasattr(raw_response, 'content'):
                        raw_text = raw_response.content
                    elif isinstance(raw_response, str):
                        raw_text = raw_response
                    else:
                        raw_text = str(raw_response)
                    logger.info(f"    🔍 LLM raw type={raw_type} | preview={raw_text[:500]}")
                except Exception as _e:
                    logger.info(f"    🔍 LLM raw repr={repr(raw_response)[:500]}")

                # 解析 JSON（容错：处理 markdown 代码块）
                result_dict = self._parse_json(raw_response)

                # Pydantic Schema 校验
                validated = self._extraction_model(**result_dict)

                # M21 管线重排：正则兜底提前到校验之前 ——
                #   Pydantic → 正则补缺 → 统一校验器
                # 保证正则补齐的字段（金额/日期/甲方等）同样受全套校验约束，
                # 堵住"补齐字段绕过校验直接落库"的漏洞
                merged_fields = postprocess_fill(validated.model_dump(), contract_text)
                report = validate_extraction(merged_fields, contract_text)

                if report.has_retryable_errors:
                    # 全量硬错误一次喂回（不逐条消耗重试轮次）
                    last_validation_errors = report.errors
                    last_error = merged_fields
                    last_report = report
                    last_merged = merged_fields
                    logger.warning(
                        f"    ❌ 第 {attempt} 次校验失败（{len(report.errors)} 项），全量喂回重试"
                    )
                    continue

                # ── 校验通过（可能仍带 fatal/warnings → 由 gatekeeper 转人工）──
                elapsed = time.time() - t0
                logger.info(
                    f"  ✅ 提取成功 | attempt={attempt} | "
                    f"system_confidence={report.system_confidence:.2f} | "
                    f"fatal={len(report.fatal_errors)} warn={len(report.warnings)} | "
                    f"elapsed={elapsed:.2f}s | rag={'✅' if rag_used else '❌'}"
                )

                return ExtractResult(
                    fields=merged_fields,
                    prompt_version=system_prompt_version,
                    system_prompt_name=self.SYSTEM_PROMPT_NAME,
                    few_shot_sources=few_shot_sources,
                    few_shot_count=len(few_shot_sources),
                    used_rag=rag_used,
                    attempt_count=attempt,
                    elapsed_seconds=elapsed,
                    validation_errors=[],
                    llm_failed=False,
                    fatal_errors=report.fatal_errors,
                    warnings=report.warnings,
                    critical_missing=report.critical_missing,
                    field_evidence=report.field_evidence,
                    system_confidence=report.system_confidence,
                    is_fallback=False,
                )

            except (json.JSONDecodeError, ValidationError) as e:
                # JSON 解析 / Schema 校验失败 → 可重试
                last_error = self._safe_jsonify(getattr(e, "json", lambda: str(e))() if isinstance(e, ValidationError) else str(e))
                last_validation_errors = (
                    [err["msg"] for err in e.errors()]
                    if isinstance(e, ValidationError) else [str(e)]
                )

                logger.warning(f"    ❌ 第 {attempt} 次失败: {e}")
                if attempt < self.MAX_RETRIES:
                    logger.info(f"    🔁 使用 {self.RETRY_PROMPT_NAME} 重试...")
                    continue
                break  # 耗尽 → 走最终结果分支

            except Exception as e:
                # LLM 调用异常（网络/超时）→ 可重试
                logger.warning(f"    ❌ 第 {attempt} 次 LLM 异常: {type(e).__name__}: {e}")
                last_validation_errors = [f"LLM 调用异常: {type(e).__name__}"]
                if attempt < self.MAX_RETRIES:
                    continue
                # LLM 异常耗尽 → 无可信成果，正则抢救 fallback
                elapsed = time.time() - t0
                logger.error(f"  ❌ 全部 {self.MAX_RETRIES} 次 LLM 异常耗尽")
                return self._build_fallback(
                    contract_text=contract_text,
                    contract_type=ctype,
                    attempt_count=attempt,
                    elapsed_seconds=elapsed,
                    few_shot_sources=few_shot_sources,
                    used_rag=rag_used,
                    validation_errors=last_validation_errors,
                    llm_failed=True,
                    prompt_version=system_prompt_version,
                )

        # ── 重试耗尽（校验类/时间熔断）：返回最后一次最佳成果 + 未解决错误标记 ──
        # M21 变更：不再丢弃成果走空壳 fallback —— 最后一次结果连同报告交
        # gatekeeper 强制转人工，比返回空壳更有价值，且保证错误必被标记
        elapsed = time.time() - t0
        if last_merged is not None:
            report = last_report or validate_extraction(last_merged, contract_text)
            logger.error(
                f"  ❌ {self.MAX_RETRIES} 次尝试耗尽 | 返回最后一次成果 | "
                f"未解决硬错误 {len(report.errors)} 项（转人工）"
            )
            return ExtractResult(
                fields=last_merged,
                prompt_version=system_prompt_version,
                system_prompt_name=self.SYSTEM_PROMPT_NAME,
                few_shot_sources=few_shot_sources,
                few_shot_count=len(few_shot_sources),
                used_rag=rag_used,
                attempt_count=attempt,
                elapsed_seconds=elapsed,
                validation_errors=report.errors,
                llm_failed=False,
                fatal_errors=report.fatal_errors,
                warnings=report.warnings,
                critical_missing=report.critical_missing,
                field_evidence=report.field_evidence,
                system_confidence=report.system_confidence,
                is_fallback=False,
            )
        # 连一次有效 JSON 都没拿到 → 正则抢救兜底
        logger.error(f"  ❌ 从未获得有效 LLM 输出，走正则抢救 fallback")
        return self._build_fallback(
            contract_text=contract_text,
            contract_type=ctype,
            attempt_count=attempt,
            elapsed_seconds=elapsed,
            few_shot_sources=few_shot_sources,
            used_rag=rag_used,
            validation_errors=last_validation_errors,
            llm_failed=False,
            prompt_version=system_prompt_version,
        )

    # 兼容旧 API（main.py / evaluate_accuracy.py 可能还在用 .extract()）
    def extract(self, contract_text: str, contract_type: str = None, use_few_shot: bool = True) -> dict:
        """旧签名兼容：直接返回 fields dict"""
        result = self.extract_contract_fields(
            contract_text, contract_type=contract_type, use_rag=use_few_shot
        )
        return result.fields

    # ═══════════════════════════════════════════════════════════════
    # JSON 解析（容错：处理 markdown 代码块、尾逗号等）
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_json(text) -> dict:
        """容错解析 LLM 输出（兼容 str / LLMResponse 对象）"""
        # ── 兼容：如果传进来是对象（如 LLMResponse），先转字符串 ──
        if hasattr(text, 'content'):
            text = text.content
        if isinstance(text, (dict, list)):
            return text  # 已经是 dict 了
        if not isinstance(text, str):
            text = str(text)

        if not text or not text.strip():
            raise json.JSONDecodeError("空响应", text, 0)

        text = text.strip()

        # 1. 优先匹配 ```json ... ```
        m = re.search(r"```json\s*(.+?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                text = m.group(1)  # 继续尝试其他方式

        # 2. 找最外层 {}
        # 先简单尝试直接 json.loads（LLM json_mode 通常能直接 parse）
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 3. 正则提取最外层 JSON 对象
        m = re.search(r"\{.+\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))

        # 4. 最后一搏
        return json.loads(text)

    # ═══════════════════════════════════════════════════════════════
    # 业务校验（兼容保留）：委托统一校验器 validator.validate_extraction
    # M21 起校验逻辑全部收敛到 validator.py（单一实现），本方法仅作
    # 旧调用方/测试的兼容薄壳，返回可重试硬错误列表
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _business_validate(data: BaseModel, raw_text: str) -> list[str]:
        report = validate_extraction(
            data.model_dump() if hasattr(data, "model_dump") else dict(data),
            raw_text,
        )
        return report.errors

    # ═══════════════════════════════════════════════════════════════
    # 兜底：重试耗尽时返回合法 Schema + 低置信度
    # ═══════════════════════════════════════════════════════════════

    def _build_fallback(
        self,
        contract_text: str,
        contract_type: str,
        attempt_count: int,
        elapsed_seconds: float,
        few_shot_sources: list[str],
        used_rag: bool,
        validation_errors: list[str],
        llm_failed: bool,
        prompt_version: str,
    ) -> ExtractResult:
        """LLM 全败时的正则抢救兜底（M21 改造）

        ⚠️ 可信度铁律：
          - contract_name 不再用 contract_text[:80] 冒充（截断文本当名字
            会污染台账）→ 置 None，由 Odoo 侧 critical_missing 逻辑拦下
          - postprocess_fill 抢救的结构化字段（编号/金额/日期/甲乙方）保留
            （来自原文正则，有据可查），但 is_fallback=True 强制走人工
        """
        fallback_fields = {
            "contract_name": None,
            "contract_code": None,
            "partner_a": None,
            "partner_b": None,
            "amount": None,
            "amount_uppercase": None,
            "currency": None,
            "sign_date": None,
            "effective_date": None,
            "expire_date": None,
            "contract_type": contract_type or "其他",
            "payment_terms": None,
            "breach_clause": None,
            "dispute_resolution": None,
            "confidence": 0.1,
        }
        # M20 正则兜底：LLM 全败时用正则抢救结构化字段（编号/金额/日期/甲乙方）
        fallback_fields = postprocess_fill(fallback_fields, contract_text)
        result = ExtractResult(
            fields=fallback_fields,
            prompt_version=prompt_version,
            system_prompt_name=self.SYSTEM_PROMPT_NAME,
            few_shot_sources=few_shot_sources,
            few_shot_count=len(few_shot_sources),
            used_rag=used_rag,
            attempt_count=attempt_count,
            elapsed_seconds=elapsed_seconds,
            validation_errors=validation_errors,
            llm_failed=llm_failed,
            is_fallback=True,
            system_confidence=0.2,
        )
        logger.warning(
            f"  🩹 fallback 抢救字段: {[k for k, v in fallback_fields.items() if v is not None]}"
        )
        return result

    @staticmethod
    def _safe_jsonify(obj) -> str:
        try:
            return json.dumps(obj, ensure_ascii=False) if isinstance(obj, (dict, list)) else str(obj)
        except Exception:
            return str(obj)

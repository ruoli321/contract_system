# ═══════════════════════════════════════════════════════════════════
# M14 · AI 服务 API 层（FastAPI 主入口）
#
# 设计原则：
#   1. 统一响应格式：{success, data, error, elapsed_ms} —— 所有端点都用
#   2. 全局异常兜底：未捕获异常也包成统一格式返回 500
#   3. 日志全覆盖：入参 + 耗时 + 异常堆栈
#   4. 延迟初始化 LLM：无 API Key 不阻塞启动，首次用时报错
#
# 核心端点（用户要求）：
#   POST /api/contract/extract  — 上传 PDF → 解析 → 分类 → 提取 → 一键全流程
#   POST /api/contract/classify  — 接收文本 → 分类
#   GET  /api/vector/search      — query 参数 → 向量检索
#
# 辅助端点：
#   GET  /api/health             — 健康检查
#   POST /api/contract/parse     — 纯 PDF 解析（调试/独立使用）
#   GET  /api/vector/stats       — 向量库统计（调试）
# ═══════════════════════════════════════════════════════════════════
import logging
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import get_settings
from .services.payment_schedule import parse_payment_terms
from .services.chinese_amount import chinese_uppercase_to_amount
from .services.clause_segmenter import segment_clauses, build_elements

# ── 日志 ──
logging.basicConfig(
    level=getattr(logging, get_settings().log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("contract-ai")

# ── 全局服务实例（生命周期管理）──
_services = {}


# ═══════════════════════════════════════════════════════════════════
# 统一响应格式 helpers
# ═══════════════════════════════════════════════════════════════════

def _ok(data=None, elapsed_ms: float = 0.0) -> dict:
    """成功响应"""
    return {"success": True, "data": data, "error": None, "elapsed_ms": round(elapsed_ms, 1)}


def _err(message: str, code: str = "INTERNAL_ERROR", elapsed_ms: float = 0.0) -> dict:
    """错误响应"""
    return {
        "success": False,
        "data": None,
        "error": {"code": code, "message": message},
        "elapsed_ms": round(elapsed_ms, 1),
    }


# ═══════════════════════════════════════════════════════════════════
# 生命周期
# ═══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化无 LLM 依赖的服务，LLM 延迟初始化"""
    settings = get_settings()
    logger.info(f"🚀 启动合同 AI 服务 | LLM={settings.llm_model} | Embedding={settings.embedding_model}")

    from .services.pdf_parser import PdfProcessor
    from .services.vector_store import VectorStore
    from .services.prompt_manager import PromptManager
    from .services.rag_learner import RAGLearner

    # ── 无 LLM 依赖，直接初始化 ──
    _services["pdf_parser"] = PdfProcessor(
        enable_table_detect=settings.ocr_enable_table_detect,
    )
    logger.info("  ✅ PDF Parser 就绪 | 表格识别: %s", "开" if settings.ocr_enable_table_detect else "关（OCR_ENABLE_TABLE_DETECT=1 可开启）")

    _services["prompt_manager"] = PromptManager(prompts_dir=settings.prompts_dir)
    logger.info(f"  ✅ Prompt Manager 就绪 | 版本: {_services['prompt_manager'].current_version}")

    _services["vector_store"] = VectorStore(
        host=settings.chroma_host,
        port=settings.chroma_port,
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
    )
    logger.info(f"  ✅ Chroma 向量库就绪 | 模型: {settings.embedding_model}")

    _services["rag_learner"] = RAGLearner(
        vector_store=_services["vector_store"],
        collection_name=settings.chroma_collection_examples,
    )
    logger.info(f"  ✅ RAG Learner 就绪 | collection: {settings.chroma_collection_examples}")

    # ── 加载标准合同范例（few-shot 数据源）──
    _app_root = Path(__file__).resolve().parent.parent  # ai-service/（容器内 = /app）
    _annot_dir = Path(settings.annotations_dir) if settings.annotations_dir else _app_root / "examples" / "annotations"
    if not _annot_dir.is_absolute():
        _annot_dir = _app_root / _annot_dir

    _texts_dir = None
    if settings.test_pdfs_dir:
        _texts_dir = Path(settings.test_pdfs_dir)
    else:
        _local_fallback = _app_root.parent / "test_pdfs"
        if _local_fallback.exists():
            _texts_dir = _local_fallback

    try:
        _load_result = _services["rag_learner"].load_examples(
            annotations_dir=str(_annot_dir),
            texts_dir=str(_texts_dir) if _texts_dir and _texts_dir.exists() else None,
            reset=False,
        )
        _success = sum(1 for r in _load_result if "error" not in r)
        _stats = _services["rag_learner"].stats()
        _type_dist = _stats.get('by_contract_type', {})
        logger.info(
            f"  ✅ 标准范例加载完成 | {_success}/{len(_load_result)} 份 | "
            f"内存 {_stats['total_examples']} 份 | "
            f"Chroma {_stats['total_chunks_in_chroma']} 块 | "
            f"按类型: {_type_dist}"
        )
        if _stats['total_examples'] < 8:
            logger.warning(
                f"  ⚠️  范例数量偏少（{_stats['total_examples']} < 8），few-shot 效果可能不明显。"
                f" 执行 docker exec contract-ai python /app/scripts/bootstrap_examples.py 补全"
            )
    except FileNotFoundError as e:
        logger.warning(f"  ⚠️  标注目录不存在，跳过范例加载: {e}")
    except ConnectionError as e:
        logger.warning(f"  ⚠️  Chroma 尚未就绪，跳过范例加载（启动后手动执行 bootstrap_examples.py）: {e}")
    except Exception as e:
        logger.warning(f"  ⚠️  范例加载异常，RAG few-shot 将不可用: {type(e).__name__}: {e}")

    # ── 延迟初始化（首次调用 LLM 端点时才创建）──
    _services["llm_client"] = None
    _services["classifier"] = None
    _services["extractor"] = None

    yield

    logger.info("👋 关闭合同 AI 服务")
    _services.clear()


# ── FastAPI App ──
app = FastAPI(
    title="合同 AI 服务",
    version="1.0.0",
    description="合同智能处理：PDF 解析 → 分类 → RAG 增强字段提取",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════════════════════
# 全局异常处理 —— 所有未捕获异常都包成统一格式
# ═══════════════════════════════════════════════════════════════════

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTPException（如手动抛的 400/500）也包成统一格式"""
    logger.warning(f"⚠️  HTTPException | path={request.url.path} | code={exc.status_code} | detail={exc.detail}")
    err_body = _err(str(exc.detail) if exc.detail else "HTTP Error", code=str(exc.status_code), elapsed_ms=0.0)
    return JSONResponse(status_code=exc.status_code, content=err_body)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """完全未捕获异常"""
    logger.exception(f"❌ 未捕获异常 | path={request.url.path} | method={request.method}")
    err_body = _err(str(exc), code="UNHANDLED_EXCEPTION", elapsed_ms=0.0)
    return JSONResponse(status_code=500, content=err_body)


# ═══════════════════════════════════════════════════════════════════
# LLM 延迟初始化（避免无 API Key 时启动失败）
# ═══════════════════════════════════════════════════════════════════

def _get_llm_client():
    """首次需要 LLM 时才初始化，失败抛 HTTPException（会被全局 handler 包成统一格式）"""
    if _services.get("llm_client") is not None:
        return _services["llm_client"], _services["classifier"], _services["extractor"]

    settings = get_settings()
    if not settings.llm_api_key:
        raise HTTPException(status_code=500, detail="LLM_API_KEY 未配置，请在 .env 中填写")

    from .services.llm_client import create_llm
    from .services.classifier import ContractClassifier
    from .services.extractor import ContractExtractor

    llm = create_llm(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
    )
    _services["llm_client"] = llm
    _services["classifier"] = ContractClassifier(
        llm_client=llm,
        prompt_manager=_services["prompt_manager"],
    )
    _services["extractor"] = ContractExtractor(
        llm_client=llm,
        prompt_manager=_services["prompt_manager"],
        rag_learner=_services.get("rag_learner"),  # 可选注入
    )
    logger.info(f"  ✅ LLM ({settings.llm_provider}@{settings.llm_model}) + Classifier + Extractor 就绪")
    return llm, _services["classifier"], _services["extractor"]


def _get_graph_services():
    """首次需要 LangGraph 时才编译图（复用 _get_llm_client 的 DI 服务）"""
    if _services.get("graph_app") is not None:
        return _services["graph_app"], _services["linear_chain"]

    _, classifier, extractor = _get_llm_client()
    from .graph import build_contract_graph
    graph_app, linear_chain = build_contract_graph(classifier, extractor)
    _services["graph_app"] = graph_app
    _services["linear_chain"] = linear_chain
    return graph_app, linear_chain


# ═══════════════════════════════════════════════════════════════════
# 1. 健康检查
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health():
    settings = get_settings()
    t0 = time.time()

    try:
        data = {
            "status": "ok",
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "embedding_model": settings.embedding_model,
            "vector_db": "chroma",
            "prompts_version": (
                _services.get("prompt_manager").current_version
                if _services.get("prompt_manager")
                else "unknown"
            ),
            "services_ready": {
                "pdf_parser": _services.get("pdf_parser") is not None,
                "prompt_manager": _services.get("prompt_manager") is not None,
                "vector_store": _services.get("vector_store") is not None,
                "rag_learner": _services.get("rag_learner") is not None,
                "llm_client": _services.get("llm_client") is not None,
            },
        }
        return _ok(data, elapsed_ms=(time.time() - t0) * 1000)
    except Exception as e:
        logger.exception("health 检查失败")
        return JSONResponse(status_code=500, content=_err(str(e), "HEALTH_CHECK_FAILED", (time.time() - t0) * 1000))


# ═══════════════════════════════════════════════════════════════════
# 2. PDF 解析（独立端点，调试用）
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/contract/parse")
async def parse_pdf(file: UploadFile = File(...)):
    """上传 PDF → 自动分流（文字版/扫描版）→ 返回文本 + 分流判定"""
    t0 = time.time()
    logger.info(f"📄 PDF 解析请求 | filename={file.filename}")

    try:
        parser = _services["pdf_parser"]
        pdf_bytes = await file.read()
        if not pdf_bytes:
            return JSONResponse(status_code=400, content=_err("上传文件为空", "EMPTY_FILE", (time.time() - t0) * 1000))

        result = parser.extract_text(pdf_bytes)
        data = {
            "filename": file.filename,
            "text": result.text,
            "pdf_type": result.pdf_type,
            "is_scanned_pdf": result.pdf_type == "scanned",
            "char_count": len(result.text),
            "tables": (
                [{"page": t.page, "rows": t.rows} for t in result.tables]
                if result.tables
                else []
            ),
        }
        elapsed = (time.time() - t0) * 1000
        logger.info(f"  ✅ PDF 解析完成 | type={result.pdf_type} | chars={len(result.text)} | {elapsed:.0f}ms")
        return _ok(data, elapsed_ms=elapsed)

    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.exception(f"PDF 解析失败 | filename={file.filename}")
        return JSONResponse(status_code=500, content=_err(f"PDF 解析失败: {str(e)}", "PDF_PARSE_FAILED", elapsed))


# ═══════════════════════════════════════════════════════════════════
# 3. 合同分类（接收文本 → 双通道融合分类）
# ═══════════════════════════════════════════════════════════════════

class ClassifyRequest(BaseModel):
    text: str = Field(..., min_length=1, description="合同文本")
    title: str = Field(default="", description="合同标题（可选）")


@app.post("/api/contract/classify")
async def classify(req: ClassifyRequest):
    """合同分类：规则匹配 + LLM 语义理解 → 双通道融合"""
    t0 = time.time()
    logger.info(f"🏷️  分类请求 | text_len={len(req.text)} | title={req.title[:20]}")

    try:
        _, classifier, _ = _get_llm_client()
        result = classifier.classify(req.text, req.title)

        data = {
            "contract_type": result.contract_type,
            "confidence": result.confidence,
            "method_used": result.method_used,
            "details": {
                "rule_result": result.rule_result,
                "rule_confidence": result.rule_confidence,
                "llm_result": result.llm_result,
                "llm_confidence": result.llm_confidence,
                "llm_failed": result.llm_failed,
            },
        }
        elapsed = (time.time() - t0) * 1000
        logger.info(f"  ✅ 分类完成 | type={result.contract_type} | conf={result.confidence:.2f} | {elapsed:.0f}ms")
        return _ok(data, elapsed_ms=elapsed)

    except HTTPException:
        raise  # 已包过统一格式（全局 handler）
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.exception("分类失败")
        return JSONResponse(status_code=500, content=_err(f"分类失败: {str(e)}", "CLASSIFY_FAILED", elapsed))


# ═══════════════════════════════════════════════════════════════════
# 4. 字段提取（PDF 上传 → 解析 → 分类 → RAG 提取 → 全流程）
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/contract/extract")
async def extract(
    file: UploadFile = File(...),
    contract_type: Optional[str] = Query(default=None, description="预设合同类型，不传则内部自动分类"),
    use_rag: bool = Query(default=True, description="是否启用 RAG few-shot 增强"),
    top_k_examples: int = Query(default=3, ge=1, le=10, description="RAG 检索范例数量"),
):
    """
    一键全流程：上传 PDF → 解析 → 自动分类（可选覆盖） → RAG 增强字段提取

    返回：
      - parse_result: PDF 解析结果（分流类型、字符数）
      - classify:     分类结果（类型 + 置信度 + 双通道详情）
      - extraction:   字段提取结果（15 字段 + 结构化日志）
    """
    t0 = time.time()
    logger.info(
        f"📋 提取请求 | filename={file.filename} | "
        f"contract_type={contract_type or 'AUTO'} | use_rag={use_rag} | top_k={top_k_examples}"
    )

    # ── Step 1: PDF 解析（单独 try，返回精确 code）──
    parser = _services["pdf_parser"]
    pdf_bytes = await file.read()
    if not pdf_bytes:
        return JSONResponse(status_code=400, content=_err("上传文件为空", "EMPTY_FILE", (time.time() - t0) * 1000))

    try:
        parse_result = parser.extract_text(pdf_bytes)
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.exception(f"PDF 解析失败 | filename={file.filename}")
        return JSONResponse(status_code=500, content=_err(f"PDF 解析失败: {str(e)}", "PDF_PARSE_FAILED", elapsed))

    text = parse_result.text
    logger.info(f"  📄 PDF 解析 | type={parse_result.pdf_type} | chars={len(text)}")

    if len(text.strip()) < 10:
        return JSONResponse(
            status_code=400,
            content=_err("PDF 解析后文本过短（<10 字符），可能是扫描版 PDF 需要 OCR", "TEXT_TOO_SHORT", (time.time() - t0) * 1000),
        )

    # ── Step 2+3: 分类 + 提取（共享 LLM 依赖，合在一个 try 里）──
    try:
        _, classifier, extractor = _get_llm_client()
        if contract_type:
            # 预设类型，跳过分类但记录一条 rule_result
            classify_result = classifier.classify(text)  # 仍然跑一次拿 rule 通道信息
            classify_data = {
                "contract_type": contract_type,
                "confidence": 0.0,  # 预设类型没有置信度
                "method_used": "preset",
                "details": {
                    "rule_result": classify_result.rule_result,
                    "llm_result": classify_result.llm_result,
                    "llm_failed": classify_result.llm_failed,
                },
                "note": f"类型由调用方预设为 '{contract_type}'，跳过自动分类",
            }
            effective_ctype = contract_type
        else:
            classify_result = classifier.classify(text)
            effective_ctype = classify_result.contract_type
            classify_data = {
                "contract_type": effective_ctype,
                "confidence": classify_result.confidence,
                "method_used": classify_result.method_used,
                "details": {
                    "rule_result": classify_result.rule_result,
                    "rule_confidence": classify_result.rule_confidence,
                    "llm_result": classify_result.llm_result,
                    "llm_confidence": classify_result.llm_confidence,
                    "llm_failed": classify_result.llm_failed,
                },
            }
        logger.info(f"  🏷️  分类 | type={effective_ctype}")

        # ── Step 3: 字段提取（新 M13 API）──
        extract_result = extractor.extract_contract_fields(
            contract_text=text,
            contract_type=effective_ctype,
            use_rag=use_rag,
            top_k_examples=top_k_examples,
        )
        extract_data = extract_result.to_api_dict()

        # B5 业财一体化：付款条款 → 收付款计划节点（规则解析，Odoo 侧据此自动生成计划）
        payment_schedule = parse_payment_terms(
            extract_data.get("payment_terms") or ""
        )
        extract_data["payment_schedule"] = payment_schedule

        # B1 大写金额交叉校验：amount_uppercase ↔ amount 语义等价性检查
        amount_check = None
        if extract_data.get("amount_uppercase"):
            upper_val = chinese_uppercase_to_amount(extract_data["amount_uppercase"])
            if upper_val is not None:
                amount_check = {
                    "uppercase_as_number": upper_val,
                    "extracted_amount": extract_data.get("amount"),
                    "match": (
                        extract_data.get("amount") is None
                        or abs(upper_val - extract_data["amount"]) < 0.01
                    ),
                }
        extract_data["amount_cross_check"] = amount_check

        # 合同条款分段 + 元素结构化（规则式，零 LLM 成本；Odoo 侧据此自动生成条款/元素记录）
        clauses = segment_clauses(text)
        elements = build_elements(extract_data, text, clauses)

        # 补充 classify 结果到同一响应
        data = {
            "filename": file.filename,
            "parse_result": {
                "pdf_type": parse_result.pdf_type,
                "is_scanned_pdf": parse_result.pdf_type == "scanned",
                "char_count": len(text),
            },
            "classify": classify_data,
            "extraction": extract_data,
            "clauses": clauses,
            "elements": elements,
        }

        elapsed = (time.time() - t0) * 1000
        logger.info(
            f"  ✅ 全流程完成 | type={effective_ctype} | "
            f"conf={extract_result.confidence:.2f} | "
            f"attempts={extract_result.attempt_count} | rag={extract_result.used_rag} | "
            f"clauses={len(clauses)} | elements={len(elements)} | "
            f"{elapsed:.0f}ms"
        )
        return _ok(data, elapsed_ms=elapsed)

    except HTTPException:
        raise
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.exception(f"提取全流程失败 | filename={file.filename}")
        return JSONResponse(
            status_code=500,
            content=_err(f"提取失败: {str(e)}", "EXTRACT_FAILED", elapsed),
        )


# ═══════════════════════════════════════════════════════════════════
# 5. 向量检索（GET query 参数版 — 用户要求的端点）
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/vector/search")
async def vector_search(
    text: str = Query(..., description="查询文本"),
    top_k: int = Query(default=3, ge=1, le=10, description="返回条数"),
    contract_type: Optional[str] = Query(default=None, description="按合同类型过滤"),
    collection: str = Query(default=None, description="目标集合名，不传用默认值"),
):
    """
    GET 向量检索：query 参数版（适合浏览器直接测试 / curl 调用）

    支持过滤：?text=xxx&contract_type=采购合同&top_k=5
    """
    t0 = time.time()
    logger.info(f"🔍 向量检索 | text_len={len(text)} | top_k={top_k} | type_filter={contract_type}")

    try:
        vs = _services["vector_store"]
        settings = get_settings()
        target_collection = collection or settings.chroma_collection_chunks
        where_filter = {"contract_type": contract_type} if contract_type else None

        results = vs.query(
            query_text=text,
            collection_name=target_collection,
            top_k=top_k,
            filter=where_filter,
        )

        data = {
            "text": text,
            "collection": target_collection,
            "total": len(results),
            "results": [
                {
                    "id": r["id"],
                    "distance": r["distance"],
                    "similarity": r["similarity"],
                    "metadata": r.get("metadata", {}),
                    "text": r["text"][:300] + ("..." if len(r["text"]) > 300 else ""),
                }
                for r in results
            ],
        }
        elapsed = (time.time() - t0) * 1000
        logger.info(f"  ✅ 检索完成 | results={len(results)} | target={target_collection} | {elapsed:.0f}ms")
        return _ok(data, elapsed_ms=elapsed)

    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.exception("向量检索失败")
        return JSONResponse(status_code=500, content=_err(f"向量检索失败: {str(e)}", "VECTOR_SEARCH_FAILED", elapsed))


# ═══════════════════════════════════════════════════════════════════
# 6. 向量库统计（调试用）
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/vector/stats")
async def vector_stats():
    """向量库统计：列出所有 collection 的 count + meta"""
    t0 = time.time()
    logger.info("📊 向量库统计请求")

    try:
        vs = _services["vector_store"]
        collections = vs.list_collections()
        stats = {}
        for name in collections:
            try:
                stats[name] = {
                    "count": vs.count(name),
                    "meta": vs.get_collection_meta(name),
                }
            except Exception as inner:
                stats[name] = {"count": -1, "error": str(inner)}

        return _ok(
            {"collections": collections, "stats": stats},
            elapsed_ms=(time.time() - t0) * 1000,
        )
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.exception("向量库统计失败")
        return JSONResponse(status_code=500, content=_err(f"统计失败: {str(e)}", "VECTOR_STATS_FAILED", elapsed))


# ═══════════════════════════════════════════════════════════════════
# 7. LangGraph 多 Agent 全流程（M19 · D3+D5 考核项）
# ═══════════════════════════════════════════════════════════════════

class GraphExtractRequest(BaseModel):
    text: str = Field(..., min_length=1, description="合同文本")
    title: str = Field(default="", description="合同标题（可选，辅助分类）")


@app.post("/api/contract/extract-graph")
async def extract_with_graph(req: GraphExtractRequest):
    """
    LangGraph 多 Agent 全流程：分类 → 提取 → 校验 →（失败回路重试）→ 审查

    与 /api/contract/extract 的区别：
      - extract 是线性直调（无校验回路）
      - extract-graph 由 LangGraph 编排，校验 Agent 独立审查必填字段，
        不通过时经 retry 节点回路重新提取（最多 2 次外重试）
    """
    t0 = time.time()
    logger.info(f"🧩 LangGraph 提取请求 | text_len={len(req.text)}")

    try:
        graph_app, _ = _get_graph_services()
        from .graph import run_contract_graph
        result = run_contract_graph(graph_app, req.text)
        elapsed = (time.time() - t0) * 1000
        logger.info(
            f"  ✅ LangGraph 完成 | type={result['contract_type']} | "
            f"retry={result['retry_count']} | errors={len(result['validation_errors'])} | {elapsed:.0f}ms"
        )
        return _ok(result, elapsed_ms=elapsed)
    except HTTPException:
        raise
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.exception("LangGraph 处理失败")
        return JSONResponse(status_code=500, content=_err(f"LangGraph 处理失败: {str(e)}", "GRAPH_FAILED", elapsed))


# ═══════════════════════════════════════════════════════════════════
# 8. LangChain Runnable 线性管道（D3 线性场景对照证据）
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/contract/extract-chain")
async def extract_with_chain(req: GraphExtractRequest):
    """
    LangChain Runnable 线性流水线：分类 → 提取（无校验回路）

    D3 选型对照：线性无状态场景用 LangChain Runnable 串行管道；
    有状态有回路的审查场景用 /api/contract/extract-graph（LangGraph）。
    """
    t0 = time.time()
    logger.info(f"🔗 LangChain 线性管道请求 | text_len={len(req.text)}")

    try:
        _, linear_chain = _get_graph_services()
        from .graph import run_linear_chain
        result = run_linear_chain(linear_chain, req.text)
        elapsed = (time.time() - t0) * 1000
        logger.info(f"  ✅ 线性管道完成 | type={result.get('contract_type')} | {elapsed:.0f}ms")
        return _ok(result, elapsed_ms=elapsed)
    except HTTPException:
        raise
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.exception("线性管道处理失败")
        return JSONResponse(status_code=500, content=_err(f"线性管道处理失败: {str(e)}", "CHAIN_FAILED", elapsed))


# ═══════════════════════════════════════════════════════════════════
# 运行入口
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

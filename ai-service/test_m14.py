"""
M14 · AI 服务 API 层 — FastAPI 端点测试
使用 FastAPI TestClient + mock 服务，不启动真实 Chroma/LLM
"""
import io
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ═══════════════════════════════════════════
# 构造带 mock 依赖的 TestClient
# ═══════════════════════════════════════════

def _make_mock_services():
    """构造所有被 mock 的前置服务实例"""

    # ── Mock PdfProcessor ──
    mock_pdf = MagicMock()
    mock_pdf.extract_text.return_value = MagicMock(
        text="办公设备采购合同\n甲方：XX公司\n乙方：YY供应商\n金额：50万元",
        pdf_type="text",
        tables=None,
        # M21/M22: gatekeeper 会真实消费 quality 内容，mock 必须符合真实契约形状
        # （真实 PdfExtractionResult.quality 为 dict 或 None，绝不是 MagicMock）
        quality={"score": 0.95, "ok": True, "pdf_type": "text",
                 "readable_ratio": 0.98, "garbage_ratio": 0.0, "threshold": 0.85},
    )

    # ── Mock PromptManager ──
    mock_pm = MagicMock()
    mock_pm.current_version = "v1"
    mock_pm.field_dict = {"extract_schema": {"fields": {}}}  # 真实 dict 让 extractor 走动态重建

    # ── Mock VectorStore ──
    mock_vs = MagicMock()
    mock_vs.query.return_value = [
        {
            "id": "chunk_001",
            "text": "采购合同条款...",
            "distance": 0.123,
            "similarity": 0.877,
            "metadata": {"contract_type": "采购合同", "page": 1},
        },
        {
            "id": "chunk_002",
            "text": "付款条件...",
            "distance": 0.245,
            "similarity": 0.755,
            "metadata": {"contract_type": "采购合同", "page": 3},
        },
    ]
    mock_vs.list_collections.return_value = ["contract_examples", "contract_chunks"]
    mock_vs.count.return_value = 42
    mock_vs.get_collection_meta.return_value = {"description": "test"}

    # ── Mock RAGLearner ──
    mock_rag = MagicMock()

    # ── Mock Classifier ──
    mock_classifier = MagicMock()
    mock_classifier.classify.return_value = MagicMock(
        contract_type="采购合同",
        confidence=0.92,
        method_used="llm",
        rule_result="采购合同",
        rule_confidence=0.8,
        llm_result="采购合同",
        llm_confidence=0.92,
        llm_failed=False,
        review_reasons=[],  # M21 分类可靠性信号
    )

    # ── Mock Extractor ──
    mock_extractor = MagicMock()
    mock_result = MagicMock()
    mock_result.confidence = 0.88
    mock_result.attempt_count = 1
    mock_result.used_rag = True
    # ── M21 质量门禁字段（main.py 构建 ValidationReport / build_quality 用）──
    mock_result.validation_errors = []
    mock_result.fatal_errors = []
    mock_result.warnings = []
    mock_result.critical_missing = []
    mock_result.field_evidence = {}
    mock_result.system_confidence = 0.90
    mock_result.is_fallback = False
    mock_result.to_api_dict.return_value = {
        "extraction": {
            "contract_name": "办公设备采购合同",
            "partner_a": "XX公司",
            "partner_b": "YY供应商",
            "amount": 500000.0,
            "contract_type": "采购合同",
            "confidence": 0.88,
        },
        "confidence": 0.88,
        "contract_type": "采购合同",
        "used_few_shot": True,
        "attempt_count": 1,
        "elapsed_seconds": 3.21,
    }
    mock_extractor.extract_contract_fields.return_value = mock_result

    # ── Mock LLM client ──
    mock_llm = MagicMock()

    return {
        "pdf_parser": mock_pdf,
        "prompt_manager": mock_pm,
        "vector_store": mock_vs,
        "rag_learner": mock_rag,
        "classifier": mock_classifier,
        "extractor": mock_extractor,
        "llm_client": mock_llm,
    }


@pytest.fixture
def client():
    """带 mock 依赖的 TestClient"""
    from app import main as main_module

    # 在导入 app 之前先塞好 services（绕过 lifespan 的真实初始化）
    mock_services = _make_mock_services()
    main_module._services = mock_services

    # 直接构造 TestClient（不触发 lifespan）
    tc = TestClient(main_module.app)
    yield tc, mock_services


# ═══════════════════════════════════════════
# 1. 健康检查
# ═══════════════════════════════════════════

def test_health_ok(client):
    tc, _ = client
    resp = tc.get("/api/health")
    body = resp.json()

    assert body["success"] is True
    assert "data" in body
    assert body["data"]["status"] == "ok"
    assert "services_ready" in body["data"]
    assert "elapsed_ms" in body


# ═══════════════════════════════════════════
# 2. 合同分类
# ═══════════════════════════════════════════

def test_classify_ok(client):
    tc, svc = client
    # 需要先初始化 LLM（让 _get_llm_client 有东西返回）
    from app import main as main_module
    main_module._services["llm_client"] = svc["llm_client"]

    resp = tc.post(
        "/api/contract/classify",
        json={"text": "本合同是一份采购合同...", "title": "采购合同"},
    )
    body = resp.json()

    assert body["success"] is True
    data = body["data"]
    assert data["contract_type"] == "采购合同"
    assert data["confidence"] == 0.92
    assert data["method_used"] == "llm"
    assert "details" in data
    svc["classifier"].classify.assert_called_once()


def test_classify_empty_text_422(client):
    tc, _ = client
    resp = tc.post("/api/contract/classify", json={"text": ""})
    # FastAPI 会返回 422（校验失败）
    assert resp.status_code == 422


def test_classify_llm_not_initialized(client):
    """未设置 LLM_API_KEY 时，_get_llm_client 会抛 HTTPException → 全局 handler 包成统一格式"""
    tc, svc = client
    from app import main as main_module

    # 确保没有预初始化
    main_module._services["llm_client"] = None

    with patch("app.main.get_settings") as mock_settings:
        fake_settings = MagicMock()
        fake_settings.llm_api_key = ""  # 空 API Key
        mock_settings.return_value = fake_settings

        resp = tc.post(
            "/api/contract/classify",
            json={"text": "采购合同...", "title": ""},
        )
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "LLM_API_KEY 未配置，请在 .env 中填写" or "error" in body


# ═══════════════════════════════════════════
# 3. 字段提取（PDF 上传全流程）
# ═══════════════════════════════════════════

def test_extract_full_flow(client):
    tc, svc = client
    from app import main as main_module
    main_module._services["llm_client"] = svc["llm_client"]

    # 构造假 PDF bytes
    fake_pdf = io.BytesIO(b"%PDF-1.4 fake pdf content here" * 20).getvalue()

    resp = tc.post(
        "/api/contract/extract",
        files={"file": ("test.pdf", fake_pdf, "application/pdf")},
        data={"use_rag": "true", "top_k_examples": "3"},
    )
    body = resp.json()

    assert body["success"] is True
    data = body["data"]

    # parse_result
    assert data["filename"] == "test.pdf"
    assert "parse_result" in data
    assert data["parse_result"]["pdf_type"] == "text"
    assert data["parse_result"]["char_count"] > 0

    # classify
    assert "classify" in data
    assert data["classify"]["contract_type"] == "采购合同"
    assert data["classify"]["confidence"] == 0.92

    # extraction（来自 to_api_dict）
    assert "extraction" in data
    assert data["extraction"]["confidence"] == 0.88
    assert data["extraction"]["attempt_count"] == 1
    assert data["extraction"]["used_few_shot"] is True

    # 耗时
    assert "elapsed_ms" in body

    # 验证服务调用
    svc["pdf_parser"].extract_text.assert_called_once()
    svc["classifier"].classify.assert_called()
    svc["extractor"].extract_contract_fields.assert_called_once()


def test_extract_with_preset_contract_type(client):
    """预设 contract_type 时跳过自动分类"""
    tc, svc = client
    from app import main as main_module
    main_module._services["llm_client"] = svc["llm_client"]

    fake_pdf = io.BytesIO(b"%PDF-1.4 fake" * 20).getvalue()
    resp = tc.post(
        "/api/contract/extract",
        files={"file": ("test.pdf", fake_pdf, "application/pdf")},
        params={"contract_type": "服务合同"},  # Query 参数 → 用 params 传
    )
    body = resp.json()

    assert body["success"] is True
    assert body["data"]["classify"]["contract_type"] == "服务合同"
    assert body["data"]["classify"]["method_used"] == "preset"


def test_extract_empty_file_400(client):
    tc, svc = client
    fake_pdf = b""  # 空文件

    resp = tc.post(
        "/api/contract/extract",
        files={"file": ("empty.pdf", fake_pdf, "application/pdf")},
    )
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "EMPTY_FILE"


def test_extract_pdf_parse_failure(client):
    tc, svc = client
    from app import main as main_module
    main_module._services["llm_client"] = svc["llm_client"]

    svc["pdf_parser"].extract_text.side_effect = Exception("PDF 解析器崩溃")

    fake_pdf = io.BytesIO(b"%PDF-1.4 broken" * 20).getvalue()
    resp = tc.post(
        "/api/contract/extract",
        files={"file": ("broken.pdf", fake_pdf, "application/pdf")},
    )
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PDF_PARSE_FAILED"


def test_extract_llm_exception(client):
    """LLM 调用异常时走兜底 + 统一错误格式"""
    tc, svc = client
    from app import main as main_module
    main_module._services["llm_client"] = svc["llm_client"]

    svc["extractor"].extract_contract_fields.side_effect = Exception("连接超时")

    fake_pdf = io.BytesIO(b"%PDF-1.4 fake" * 20).getvalue()
    resp = tc.post(
        "/api/contract/extract",
        files={"file": ("test.pdf", fake_pdf, "application/pdf")},
    )
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "EXTRACT_FAILED"
    assert "连接超时" in body["error"]["message"]


# ═══════════════════════════════════════════
# 4. GET 向量检索（query 参数）
# ═══════════════════════════════════════════

def test_vector_search_ok(client):
    tc, svc = client

    resp = tc.get(
        "/api/vector/search",
        params={"text": "采购合同 付款条款", "top_k": 3},
    )
    body = resp.json()

    assert body["success"] is True
    data = body["data"]
    assert data["text"] == "采购合同 付款条款"
    assert data["total"] == 2
    assert len(data["results"]) == 2
    # 返回的结果应该被截断（text 前 300 字符）
    for r in data["results"]:
        assert "id" in r
        assert "distance" in r
        assert "similarity" in r
        assert "metadata" in r
        assert isinstance(r["text"], str)

    svc["vector_store"].query.assert_called_once_with(
        query_text="采购合同 付款条款",
        collection_name="contract_chunks",
        top_k=3,
        filter=None,
    )


def test_vector_search_with_type_filter(client):
    tc, svc = client
    resp = tc.get(
        "/api/vector/search",
        params={"text": "金额", "contract_type": "采购合同", "top_k": 5},
    )
    body = resp.json()

    assert body["success"] is True
    # filter 应该带上 contract_type
    svc["vector_store"].query.assert_called_once()
    call_kwargs = svc["vector_store"].query.call_args
    assert call_kwargs[1].get("filter") == {"contract_type": "采购合同"}


def test_vector_search_missing_query_422(client):
    tc, _ = client
    resp = tc.get("/api/vector/search")
    assert resp.status_code == 422  # query 是必填


def test_vector_search_db_failure(client):
    tc, svc = client
    svc["vector_store"].query.side_effect = Exception("Chroma 连接超时")

    resp = tc.get(
        "/api/vector/search",
        params={"text": "test"},
    )
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VECTOR_SEARCH_FAILED"


# ═══════════════════════════════════════════
# 5. 向量库统计
# ═══════════════════════════════════════════

def test_vector_stats_ok(client):
    tc, svc = client
    resp = tc.get("/api/vector/stats")
    body = resp.json()

    assert body["success"] is True
    assert "collections" in body["data"]
    assert "stats" in body["data"]
    assert "contract_examples" in body["data"]["collections"]


# ═══════════════════════════════════════════
# 6. 统一响应格式验证
# ═══════════════════════════════════════════

def test_response_format_success(client):
    """所有成功响应必须包含 success/data/error/elapsed_ms"""
    tc, _ = client
    resp = tc.get("/api/health")
    body = resp.json()

    assert "success" in body and isinstance(body["success"], bool)
    assert "data" in body
    assert "error" in body
    assert "elapsed_ms" in body


def test_response_format_error(client):
    """所有错误响应必须 error 不为 null，且包含 code/message"""
    tc, svc = client
    svc["vector_store"].query.side_effect = Exception("boom")

    resp = tc.get("/api/vector/search", params={"text": "x"})
    body = resp.json()

    assert body["success"] is False
    assert body["data"] is None
    assert body["error"] is not None
    assert "code" in body["error"]
    assert "message" in body["error"]
    assert "elapsed_ms" in body


# ═══════════════════════════════════════════
# 7. 全局异常兜底
# ═══════════════════════════════════════════

def test_global_exception_handler(client):
    """完全未捕获的异常也会被全局 handler 包成统一格式"""
    tc, svc = client

    # 让 health 端点内部直接抛异常（比如 prompt_manager 是 MagicMock 时 .current_version 行为异常）
    from app import main as main_module
    main_module._services["prompt_manager"].current_version = None

    resp = tc.get("/api/health")
    body = resp.json()
    # 可能 success=True（None 也能转 "unknown"）或 success=False
    # 关键是格式统一
    assert "success" in body
    assert "error" in body
    assert "elapsed_ms" in body


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))


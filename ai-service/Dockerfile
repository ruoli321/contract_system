# ═══════════════════════════════════════════
# AI 服务 Dockerfile（FastAPI + LangChain + PaddleOCR + OpenCV）
# 基础镜像用 3.11-slim（PaddleOCR 对 3.13 支持有限）
# ═══════════════════════════════════════════

FROM python:3.11-slim

# ── 系统依赖 ──
# libgl1: OpenCV 运行时（新版 Debian 统一包名，不要用 libgl1-mesa-glx）
# libglib2.0-0: PaddlePaddle 运行时
# poppler-utils: pdf2image 兜底方案（PyMuPDF 不需要，但装了更保险）
# curl: healthcheck 用
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    poppler-utils \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── 工作目录 ──
WORKDIR /app

# ── 依赖先装 requirements（利用 Docker 缓存层）──
# 默认 PyPI 官方源 + 阿里云镜像（国内加速）+ 5分钟超时
COPY requirements.txt .
RUN pip install --no-cache-dir \
    --index-url https://pypi.org/simple/ \
    --extra-index-url https://mirrors.aliyun.com/pypi/simple/ \
    --default-timeout=300 \
    -r requirements.txt

# ── 应用代码 ──
COPY app/ ./app/
COPY prompts/ ./prompts/
COPY scripts/ ./scripts/

# ── 端口 ──
EXPOSE 8000

# ── 启动命令（uvicorn，生产级）──
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

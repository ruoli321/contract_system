# ═══════════════════════════════════════════
# 合同管理系统 - 一键启动脚本（Windows PowerShell）
# ═══════════════════════════════════════════

Write-Host "════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  合同管理系统 - Docker Compose 一键启动" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# 1. 检查 Docker 是否运行
Write-Host "[1/3] 检查 Docker 引擎..." -ForegroundColor Yellow
try {
    docker info | Out-Null
    Write-Host "  ✅ Docker 引擎正常" -ForegroundColor Green
} catch {
    Write-Host "  ❌ Docker 未运行！请先启动 Docker Desktop" -ForegroundColor Red
    exit 1
}

# 2. 检查 .env 文件
Write-Host "[2/3] 检查 .env 配置..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    Write-Host "  ⚠️  .env 不存在，从 .env.example 复制一份" -ForegroundColor Yellow
    Copy-Item .env.example .env
    Write-Host "  ✅ 已创建 .env，请填入真实 API Key 后重新运行" -ForegroundColor Yellow
    Write-Host "     位置: $(Resolve-Path .env)" -ForegroundColor Yellow
    exit 0
} else {
    Write-Host "  ✅ .env 已存在" -ForegroundColor Green
}

# 3. 启动服务
Write-Host "[3/3] 启动所有服务..." -ForegroundColor Yellow
Write-Host ""
docker compose up -d --build

Write-Host ""
Write-Host "════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  ✅ 启动完成！" -ForegroundColor Green
Write-Host ""
Write-Host "  📊 查看状态:  docker compose ps" -ForegroundColor White
Write-Host "  📋 查看日志:  docker compose logs -f" -ForegroundColor White
Write-Host "  🛑 停止服务:  docker compose down" -ForegroundColor White
Write-Host ""
Write-Host "  🌐 Odoo:       http://localhost:8069" -ForegroundColor Cyan
Write-Host "     默认账号: admin / admin" -ForegroundColor Cyan
Write-Host ""
Write-Host "  🔌 AI 服务:     http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "     健康检查:  http://localhost:8000/api/health" -ForegroundColor Cyan
Write-Host ""
Write-Host "  📖 安装 Odoo 模块：" -ForegroundColor Yellow
Write-Host "     1. 登录 Odoo → 设置 → 应用" -ForegroundColor Yellow
Write-Host "     2. 开启开发者模式（顶部灰色条点击）" -ForegroundColor Yellow
Write-Host "     3. 点击「更新应用列表」" -ForegroundColor Yellow
Write-Host "     4. 搜索「合同」→ 安装「合同管理系统」" -ForegroundColor Yellow
Write-Host "════════════════════════════════════════════" -ForegroundColor Green

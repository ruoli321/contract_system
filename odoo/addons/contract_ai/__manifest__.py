{
    "name": "合同管理 AI 模块",
    "version": "17.0.1.0.0",
    "summary": "企业级合同管理 + AI 智能提取/分类/审查",
    "description": """
    合同管理 AI 模块
    ═══════════════════════════════════════════
    核心能力：
    1. PDF 自动解析分流（文字版 pdfplumber / 扫描版 PaddleOCR）
    2. AI 智能字段提取（RAG few-shot + JSON Schema 约束 + 重试兜底）
    3. 合同自动分类（规则 + LLM 双通道 + 置信度融合）
    4. 状态机流转（草拟 → 审批 → 用印 → 归档 / 作废）
    5. 业财一体化（付款节点 → 收付款计划 → 台账联动）
    6. 配置化编号规则 + Excel 台账导出（M17）

    架构：Odoo 业务层 + AI 微服务（FastAPI + LangChain + Chroma）
    """,
    "author": "Contract AI Team",
    "license": "LGPL-3",
    "category": "Documents/Document Management",
    "depends": [
        "base",
        "mail",
        "web",
    ],
    "data": [
        "security/contract_ai_security.xml",
        "security/ir.model.access.csv",
        # 审批流服务端动作（P0-2：Odoo 17 无 workflow，用 ir.actions.server）
        "security/contract_workflow_actions.xml",
        "data/contract_sequence.xml",
        # 定时任务（P0-2：逾期提醒 + 到期自动归档）
        "data/contract_cron.xml",
        "wizard/contract_upload_wizard.xml",
        "views/contract_views.xml",
        "views/contract_counterparty_views.xml",
        "views/contract_signatory_views.xml",
        "views/contract_template_views.xml",
        "views/contract_payment_plan_views.xml",
        "views/contract_report_views.xml",
        "views/contract_config_views.xml",
        "views/contract_menu.xml",
        "report/contract_pdf_reports.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}

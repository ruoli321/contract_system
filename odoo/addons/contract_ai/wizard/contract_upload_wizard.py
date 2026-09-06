# ════════════════════════════════════════════════
# contract.upload.wizard — 一键上传 PDF + AI 提取向导
# ════════════════════════════════════════════════
import base64
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ContractUploadWizard(models.TransientModel):
    """
    一键上传 PDF + AI 提取向导。

    使用流程:
      1. 用户在合同台账列表页点击「📄 上传 PDF 并 AI 提取」按钮
      2. 在向导里选择 PDF 文件
      3. 点击「开始识别」→ 立即创建 contract.contract 记录并跳转表单
      4. 后台线程异步调用 AI 服务 /api/contract/extract（扫描件 OCR 约 1~2 分钟）
      5. 识别完成后字段自动回填 + chatter 通知

    ⚠️ 为什么异步：扫描版 PDF 走 OCR + LLM 全流程约 60~120 秒，
       同步等待会撞 requests 超时和 Odoo worker 限制，导致识别失败。
    """

    _name = "contract.upload.wizard"
    _description = "合同 PDF 上传向导"

    # ── 字段 ──
    pdf_file = fields.Binary(
        string="选择合同 PDF 文件", required=True,
        help="支持 PDF 格式，大小不超过 10MB",
    )
    pdf_filename = fields.Char(string="文件名")

    # ── 主方法：创建合同 + 后台异步 AI 提取 ──
    def action_create_and_extract(self):
        """
        一键完成：创建草稿合同 → 上传 PDF → 后台异步调 AI → 跳表单
        """
        self.ensure_one()
        if not self.pdf_file:
            raise UserError(_("请先选择一个 PDF 文件"))

        pdf_bytes = base64.b64decode(self.pdf_file)
        if len(pdf_bytes) > 10 * 1024 * 1024:
            raise UserError(_("文件太大，超过 10MB 限制"))

        # ── Step 1: 创建一条合同记录 ──
        contract_vals = {
            "name": f"[待识别] {self.pdf_filename or '新合同'}",
            "source_pdf": self.pdf_file,
            "source_pdf_filename": self.pdf_filename or "contract.pdf",
            "state": "draft",
        }
        contract = self.env["contract.contract"].sudo().create(contract_vals)
        _logger.info("向导创建合同: id=%s, name=%s", contract.id, contract.name)

        # ── Step 2: 后台线程异步调 AI 提取（立即返回，不阻塞浏览器）──
        contract.message_post(
            body=_("🚀 已提交 AI 识别：正在后台解析与提取（扫描件约 1~2 分钟），"
                   "完成后字段自动回填，可稍后刷新查看"),
            subtype_id=self.env.ref("mail.mt_comment").id,
        )
        contract._spawn_ai_extraction()

        # ── Step 3: 跳转到新建合同的表单视图 ──
        # ⚠️ 必须返回单个 action dict：Odoo 17 前端 doAction 不接受 action 数组，
        #    返回 list 会导致跳转失败、页面空白（用户误以为上传失败）
        return {
            "type": "ir.actions.act_window",
            "res_model": "contract.contract",
            "res_id": contract.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_cancel(self):
        """取消向导"""
        return {"type": "ir.actions.act_window_close"}

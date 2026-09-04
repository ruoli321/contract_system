# ════════════════════════════════════════════════
# contract.upload.wizard — 一键上传 PDF + AI 提取向导
# ════════════════════════════════════════════════
import base64
import logging
import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ContractUploadWizard(models.TransientModel):
    """
    一键上传 PDF + AI 提取向导。

    使用流程:
      1. 用户在合同台账列表页点击「📄 上传 PDF 并 AI 提取」按钮
      2. 在向导里选择 PDF 文件
      3. 点击「开始识别」→ 自动创建 contract.contract 记录
      4. 自动调用 AI 服务的 /api/contract/extract
      5. 成功后跳转到新建合同的表单视图
    """

    _name = "contract.upload.wizard"
    _description = "合同 PDF 上传向导"

    # ── 字段 ──
    pdf_file = fields.Binary(
        string="选择合同 PDF 文件", required=True,
        help="支持 PDF 格式，大小不超过 10MB",
    )
    pdf_filename = fields.Char(string="文件名")

    # ── 主方法：创建合同 + AI 提取 ──
    def action_create_and_extract(self):
        """
        一键完成：创建草稿合同 → 上传 PDF → 调 AI → 跳表单
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

        # ── Step 2: 调 AI 服务提取 ──
        try:
            ai_url = contract._get_ai_service_url()
            files = {"file": (self.pdf_filename or "contract.pdf", pdf_bytes)}
            resp = requests.post(
                f"{ai_url}/api/contract/extract", files=files, timeout=60,
            )
            if resp.status_code != 200:
                _logger.warning("AI 提取失败 (HTTP %s): %s", resp.status_code, resp.text)
                contract.message_post(
                    body=_("⚠️ AI 自动提取失败 (HTTP %s)，请手动填写") % resp.status_code,
                    subtype_id=self.env.ref("mail.mt_comment").id,
                )
            else:
                raw = resp.json()
                # ⚠️ AI 服务返回统一格式 {"success": true, "data": {...}, ...}
                if raw.get("success"):
                    data = raw.get("data", raw)
                    contract._apply_ai_result(data)
                else:
                    contract.message_post(
                        body=_("⚠️ AI 提取服务返回错误: %s") % raw.get("error", {}).get("message", "-"),
                        subtype_id=self.env.ref("mail.mt_comment").id,
                    )

        except requests.exceptions.ConnectionError as e:
            _logger.error("AI 服务连接失败: %s", e)
            contract.message_post(
                body=_("⚠️ 无法连接 AI 服务，请检查 AI 服务是否启动"),
                subtype_id=self.env.ref("mail.mt_comment").id,
            )
        except Exception as e:
            _logger.exception("AI 提取过程异常")
            contract.message_post(
                body=_("⚠️ AI 提取异常: %s") % str(e),
                subtype_id=self.env.ref("mail.mt_comment").id,
            )

        # ── Step 3: 跳转到新建合同的表单视图 ──
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

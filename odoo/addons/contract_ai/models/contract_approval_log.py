# ════════════════════════════════════════════════
# contract.approval.log — 合同审批日志（P0-2 审批流）
# 对应肇新 contract_audit_form + ContractPointLog 设计
# ════════════════════════════════════════════════
from odoo import models, fields


class ContractApprovalLog(models.Model):
    """合同审批记录：每次提交/通过/驳回/撤回都落一条，作为 B3 审批流证据"""
    _name = "contract.approval.log"
    _description = "合同审批日志"
    _order = "create_date desc"

    contract_id = fields.Many2one(
        "contract.contract", required=True, ondelete="cascade",
        string="合同", index=True,
    )
    approver_id = fields.Many2one(
        "res.users", required=True, string="审批人",
        default=lambda self: self.env.user,
    )
    action = fields.Selection(
        [
            ("submit", "提交审批"),
            ("approve", "审批通过"),
            ("reject", "审批驳回"),
            ("cancel", "撤回"),
            # M22 复核动作（质量门禁闭环）
            ("review_confirm", "复核确认"),
            ("review_reset", "重置复核"),
        ],
        required=True, string="动作",
    )
    comment = fields.Text(string="审批意见")

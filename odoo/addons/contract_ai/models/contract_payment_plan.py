# ════════════════════════════════════════════════
# contract.payment.plan — 收付款计划（合同子表）
# M16 · 业财一体化模块
# ════════════════════════════════════════════════
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class ContractPaymentPlan(models.Model):
    """合同收付款计划"""
    _name = "contract.payment.plan"
    _description = "收付款计划"
    _order = "contract_id, sort_order"

    # ════════════════════════════════════════════════
    # 关联：必须属于一份合同
    # ════════════════════════════════════════════════
    contract_id = fields.Many2one(
        "contract.contract", string="所属合同",
        required=True, ondelete="cascade", index=True,
    )

    # ── 从合同继承的辅助字段（用于 Pivot 分组/搜索） ──
    contract_type = fields.Selection(
        related="contract_id.type", string="合同类型", store=True,
    )
    contract_code = fields.Char(
        related="contract_id.code", string="合同编号", store=True,
    )
    contract_name = fields.Char(
        related="contract_id.name", string="合同名称", store=False,
    )
    contract_amount = fields.Monetary(
        related="contract_id.amount", string="合同总额",
        currency_field="currency_id", store=True,
    )
    date_signed = fields.Date(
        related="contract_id.date_signed", string="合同签订日", store=True,
    )

    # ════════════════════════════════════════════════
    # 基本字段
    # ════════════════════════════════════════════════
    name = fields.Char(string="收付款节点", required=True, default="第 1 期")
    description = fields.Text(
        string="节点说明",
        help="该收付款节点的详细描述，如支付条件、验收标准等",
    )
    milestone = fields.Selection(
        [
            ("prepayment", "预付款"),
            ("on_delivery", "到货验收"),
            ("on_completion", "完工验收"),
            ("installment", "分期支付"),
            ("final", "尾款"),
            ("other", "其他"),
        ],
        string="节点类型", default="other",
    )
    sort_order = fields.Integer(string="序号", default=1)

    # ── 来源（B5 业财一体化：AI 提取自动生成 vs 人工录入） ──
    source = fields.Selection(
        [
            ("ai_extracted", "AI 提取生成"),
            ("manual", "人工录入"),
        ],
        string="来源", default="manual", index=True,
        help="AI 提取自动生成的计划在重新提取时会被覆盖更新；人工录入的计划不受影响",
    )

    # ── 收付款方向 ──
    direction = fields.Selection(
        [
            ("payable", "应付（付款）"),
            ("receivable", "应收（收款）"),
        ],
        string="方向",
        compute="_compute_direction", store=True,
        help="根据合同类型自动判定：采购/租赁→应付；销售/服务→应收",
    )

    # ── 计划金额与日期 ──
    currency_id = fields.Many2one(
        "res.currency", string="币种",
        related="contract_id.currency_id", store=True, readonly=False,
    )
    planned_amount = fields.Monetary(
        string="计划金额", currency_field="currency_id", required=True,
    )
    planned_date = fields.Date(string="计划日期", required=True)

    # ── 实际金额与日期 ──
    actual_amount = fields.Monetary(
        string="实际金额", currency_field="currency_id",
    )
    actual_date = fields.Date(string="实际日期")

    # ── 付款方式 ──
    payment_method = fields.Selection(
        [
            ("bank_transfer", "银行转账"),
            ("check", "支票"),
            ("cash", "现金"),
            ("letter_of_credit", "信用证"),
            ("other", "其他"),
        ],
        string="结算方式",
    )

    # ════════════════════════════════════════════════
    # 状态机：draft → confirmed → paid / cancelled
    # ════════════════════════════════════════════════
    state = fields.Selection(
        [
            ("draft", "草稿"),
            ("confirmed", "已确认"),
            ("partial", "部分完成"),
            ("paid", "已完成"),
            ("cancelled", "已取消"),
        ],
        string="状态", default="draft", tracking=True, copy=False,
    )

    # ── 自动计算标志 ──
    remaining_amount = fields.Monetary(
        string="剩余金额", currency_field="currency_id",
        compute="_compute_remaining_amount", store=True,
    )
    is_overdue = fields.Boolean(
        string="是否逾期", compute="_compute_is_overdue", store=True,
    )

    # ════════════════════════════════════════════════
    # 自动计算
    # ════════════════════════════════════════════════
    @api.depends("planned_amount", "actual_amount")
    def _compute_remaining_amount(self):
        for rec in self:
            paid = rec.actual_amount or 0.0
            rec.remaining_amount = rec.planned_amount - paid

    @api.depends("planned_date", "actual_date", "state")
    def _compute_is_overdue(self):
        from datetime import date as _date
        today = _date.today()
        for rec in self:
            if rec.state in ("paid", "cancelled"):
                rec.is_overdue = False
            elif rec.planned_date and rec.planned_date < today and not rec.actual_date:
                rec.is_overdue = True
            else:
                rec.is_overdue = False

    @api.depends("contract_id", "contract_id.type")
    def _compute_direction(self):
        """根据合同类型自动判定收/付方向"""
        payable_types = ("purchase", "lease")  # 采购、租赁 → 我方付款
        receivable_types = ("sale", "service", "labor", "partnership", "consulting")
        for rec in self:
            ctype = rec.contract_id.type if rec.contract_id else False
            if ctype in payable_types:
                rec.direction = "payable"
            elif ctype in receivable_types:
                rec.direction = "receivable"
            else:
                rec.direction = False

    # ════════════════════════════════════════════════
    # 状态流转按钮
    # ════════════════════════════════════════════════
    def action_confirm(self):
        """草稿 → 已确认（进入执行队列）"""
        for rec in self:
            if rec.state not in ("draft", "cancelled"):
                raise UserError(_("只有草稿或已取消的计划才能确认（当前: %s）") %
                                dict(self._fields["state"].get_description(self.env)["selection"]).get(rec.state, rec.state))
            rec.state = "confirmed"
            _logger.info("付款计划 %s 确认", rec.name)

    def action_done(self):
        """已确认/部分完成 → 已完成（自动填充实际金额/日期）"""
        today = fields.Date.today()
        for rec in self:
            if rec.state in ("paid", "cancelled"):
                continue
            if not rec.actual_amount:
                rec.actual_amount = rec.planned_amount
            if not rec.actual_date:
                rec.actual_date = today
            rec.state = "paid"
            _logger.info("付款计划 %s 完成 | %.2f 元 @ %s",
                         rec.name, rec.actual_amount or 0, rec.actual_date)

    def action_cancel(self):
        """任意非终态 → 已取消"""
        for rec in self:
            if rec.state in ("paid", "cancelled"):
                continue
            rec.state = "cancelled"

    def action_reset_draft(self):
        """已确认/部分完成/已取消 → 草稿"""
        for rec in self:
            if rec.state == "paid":
                raise UserError(_("已完成的计划不能重置为草稿"))
            rec.state = "draft"

    # ════════════════════════════════════════════════
    # 校验
    # ════════════════════════════════════════════════
    @api.constrains("planned_amount")
    def _check_amount_positive(self):
        for rec in self:
            if rec.planned_amount <= 0:
                raise ValidationError(_("计划金额必须大于 0"))

# ════════════════════════════════════════════════
# contract.signatory — 签约方（自然人）
# ════════════════════════════════════════════════
from odoo import models, fields, api, _


class ContractSignatory(models.Model):
    """合同签约方（我方签约人档案）"""
    _name = "contract.signatory"
    _description = "签约方"
    _inherit = ["mail.thread"]
    _order = "name"

    # ── 基本信息 ──
    name = fields.Char(string="姓名", required=True, tracking=True)
    job_title = fields.Char(string="职务 / 岗位", tracking=True)
    department = fields.Char(string="部门")

    # ── 联系方式 ──
    phone = fields.Char(string="联系电话")
    mobile = fields.Char(string="手机号码")
    email = fields.Char(string="电子邮箱")

    # ── 公司代码 ──
    company_code = fields.Char(
        string="公司代码",
        help="公司内部员工编号 / 工号 / 代码",
    )
    company_id = fields.Many2one(
        "res.company", string="所属公司",
        default=lambda self: self.env.company,
    )

    # ── 关联 Odoo 用户 ──
    user_id = fields.Many2one(
        "res.users", string="关联用户",
        ondelete="set null",
        help="如果该签约人是系统用户，可关联到此字段",
    )

    # ── 印章信息 ──
    seal_name = fields.Char(
        string="印章名称",
        help="如「合同专用章」「公章」「法人章」等",
    )
    seal_info = fields.Text(
        string="印章信息",
        help="印章编号、材质、启用日期、保管人等附加信息",
    )

    # ── 合同统计 ──
    contract_ids = fields.One2many(
        "contract.contract", "signatory_id", string="签署的合同",
    )
    contract_count = fields.Integer(
        string="已签合同数", compute="_compute_contract_count",
    )

    @api.depends("contract_ids")
    def _compute_contract_count(self):
        for rec in self:
            rec.contract_count = len(rec.contract_ids)

    def action_view_contracts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("签署的合同"),
            "res_model": "contract.contract",
            "domain": [("signatory_id", "=", self.id)],
            "view_mode": "tree,form",
        }

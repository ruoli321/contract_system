# ════════════════════════════════════════════════
# contract.counterparty — 合同相对方
# （公司 / 个人等签约主体档案）
# ════════════════════════════════════════════════
from odoo import models, fields, api, _


class ContractCounterparty(models.Model):
    """合同相对方档案"""
    _name = "contract.counterparty"
    _description = "合同相对方"
    _inherit = ["mail.thread"]
    _order = "name"

    # ── 基本信息 ──
    name = fields.Char(
        string="相对方名称", required=True, tracking=True,
        help="公司全称或个人姓名",
    )
    party_type = fields.Selection(
        [("company", "公司/企业"), ("individual", "个人"), ("other", "其他")],
        string="主体类型", default="company", tracking=True,
    )
    is_internal = fields.Boolean(
        string="内部主体", default=False,
        help="勾选表示我方内部部门或关联公司",
    )

    # ── 证照信息 ──
    unified_social_code = fields.Char(
        string="统一社会信用代码",
        help="18 位统一社会信用代码 / 组织机构代码",
    )
    tax_number = fields.Char(string="纳税人识别号 / 税号")
    legal_representative = fields.Char(string="法定代表人")

    # ── 联系信息 ──
    address = fields.Text(string="注册地址")
    contact_name = fields.Char(string="联系人")
    contact_phone = fields.Char(string="联系电话")
    contact_email = fields.Char(string="电子邮箱")
    website = fields.Char(string="公司网站")

    # ── 银行信息 ──
    bank_name = fields.Char(string="开户银行")
    bank_account = fields.Char(string="银行账号")
    bank_branch = fields.Char(string="开户支行")

    # ── 关联合同 ──
    contract_ids_a = fields.One2many(
        "contract.contract", "partner_a", string="作为甲方的合同",
    )
    contract_ids_b = fields.One2many(
        "contract.contract", "partner_b", string="作为乙方的合同",
    )
    contract_count = fields.Integer(
        string="合同数量", compute="_compute_contract_count",
    )

    @api.depends("contract_ids_a", "contract_ids_b")
    def _compute_contract_count(self):
        for rec in self:
            rec.contract_count = len(rec.contract_ids_a) + len(rec.contract_ids_b)

    # ── 快速跳转 ──
    def action_view_contracts(self):
        self.ensure_one()
        ids = (self.contract_ids_a | self.contract_ids_b).ids
        return {
            "type": "ir.actions.act_window",
            "name": _("关联合同"),
            "res_model": "contract.contract",
            "domain": [("id", "in", ids)],
            "view_mode": "tree,form",
        }

    _sql_constraints = [
        (
            "name_uniq",
            "unique(name)",
            "相对方名称已存在，请检查是否重复录入",
        ),
    ]

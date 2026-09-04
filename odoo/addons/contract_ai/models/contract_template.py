# ════════════════════════════════════════════════
# contract.template  — 合同模板
# contract.clause    — 合同条款（既可属于模板也可属于具体合同）
# ════════════════════════════════════════════════
from odoo import models, fields, api, _


class ContractTemplate(models.Model):
    """合同模板"""
    _name = "contract.template"
    _description = "合同模板"
    _order = "name"

    # ── 基本信息 ──
    name = fields.Char(string="模板名称", required=True, tracking=True)
    type = fields.Selection(
        [
            ("purchase", "采购合同"),
            ("sale", "销售合同"),
            ("service", "服务合同"),
            ("lease", "租赁合同"),
            ("labor", "劳动合同"),
            ("consulting", "咨询合同"),
            ("other", "其他"),
        ],
        string="适用合同类型", required=True, tracking=True,
    )
    description = fields.Text(string="模板说明")

    # ── 版本管理 ──
    version = fields.Char(string="版本号", default="v1.0", tracking=True)
    is_active = fields.Boolean(string="启用", default=True, tracking=True)

    # ── 使用统计 ──
    usage_count = fields.Integer(string="使用次数", default=0)

    # ── 条款（One2many → contract.clause, template_id） ──
    clause_ids = fields.One2many(
        "contract.clause", "template_id", string="模板条款",
    )
    clause_count = fields.Integer(
        string="条款数", compute="_compute_clause_count",
    )

    @api.depends("clause_ids")
    def _compute_clause_count(self):
        for rec in self:
            rec.clause_count = len(rec.clause_ids)


class ContractClause(models.Model):
    """合同条款
    - 可以属于一个模板（template_id）
    - 也可以属于一份具体合同（contract_id）
    - 从模板应用到合同时：template_id 保留来源，contract_id 指向目标合同
    """
    _name = "contract.clause"
    _description = "合同条款"
    _order = "contract_id, template_id, sort_order"

    # ════════════════════════════════════════════════
    # 关联
    # ════════════════════════════════════════════════
    # 属于哪份合同（子表 inverse）
    contract_id = fields.Many2one(
        "contract.contract", string="所属合同",
        ondelete="cascade", index=True,
    )
    # 来源于哪个模板
    template_id = fields.Many2one(
        "contract.template", string="来源模板",
        ondelete="cascade", index=True,
    )

    # ════════════════════════════════════════════════
    # 条款内容
    # ════════════════════════════════════════════════
    name = fields.Char(string="条款标题", required=True)
    clause_type = fields.Selection(
        [
            ("payment", "付款条款"),
            ("liability", "违约责任"),
            ("dispute", "争议解决"),
            ("confidential", "保密条款"),
            ("termination", "终止条款"),
            ("warranty", "质保条款"),
            ("intellectual_property", "知识产权"),
            ("force_majeure", "不可抗力"),
            ("other", "其他"),
        ],
        string="条款类型", default="other",
    )
    content = fields.Text(string="条款正文", required=True)
    sort_order = fields.Integer(string="排序", default=10)

    # ── 条款属性 ──
    is_required = fields.Boolean(
        string="必备条款", default=False,
        help="模板中标记为必备的条款在应用到合同时不可删除",
    )
    is_modified = fields.Boolean(
        string="已修改", default=False, copy=False,
        help="从模板复制到合同后是否被编辑过",
    )

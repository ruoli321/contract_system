# ════════════════════════════════════════════════
# contract.element — 合同元素
# AI 从合同中可结构化提取的原子字段（可作为 RAG few-shot 的训练数据）
# ════════════════════════════════════════════════
from odoo import models, fields, api


class ContractElement(models.Model):
    """合同元素 — 可结构化提取的原子字段"""
    _name = "contract.element"
    _description = "合同元素"
    _order = "contract_id, group, element_key"

    # ════════════════════════════════════════════════
    # 关联
    # ════════════════════════════════════════════════
    contract_id = fields.Many2one(
        "contract.contract", string="所属合同",
        required=True, ondelete="cascade", index=True,
    )
    clause_id = fields.Many2one(
        "contract.clause", string="所属条款",
        ondelete="set null", index=True,
        help="该元素隶属于合同的哪一条款（可选）",
    )

    # ════════════════════════════════════════════════
    # 元素标识
    # ════════════════════════════════════════════════
    name = fields.Char(
        string="元素名称", required=True,
        help="人类可读名称，如「合同金额」",
    )
    element_key = fields.Char(
        string="元素 Key", required=True,
        help="机器可读标识符（snake_case），如 contract_amount",
    )
    group = fields.Selection(
        [
            ("basic", "基本信息"),
            ("party", "合同主体"),
            ("amount", "金额相关"),
            ("date", "日期相关"),
            ("payment", "付款条款"),
            ("liability", "违约条款"),
            ("dispute", "争议解决"),
            ("other", "其他"),
        ],
        string="分组", default="basic",
    )

    # ════════════════════════════════════════════════
    # 值类型与实际值（多态存储）
    # ════════════════════════════════════════════════
    element_type = fields.Selection(
        [
            ("text", "文本"),
            ("number", "数字"),
            ("date", "日期"),
            ("selection", "枚举值"),
            ("boolean", "布尔值"),
            ("json", "JSON 结构"),
        ],
        string="值类型", default="text", required=True,
    )
    value_text = fields.Text(string="文本值")
    value_number = fields.Float(string="数字值")
    value_date = fields.Date(string="日期值")
    value_json = fields.Json(string="JSON 值")
    value_boolean = fields.Boolean(string="布尔值")

    # ════════════════════════════════════════════════
    # 默认值（多态，与 value_* 同结构）
    # ════════════════════════════════════════════════
    default_value_text = fields.Text(string="默认文本值")
    default_value_number = fields.Float(string="默认数字值")
    default_value_date = fields.Date(string="默认日期值")
    default_value_boolean = fields.Boolean(string="默认布尔值")
    default_value_json = fields.Json(string="默认 JSON 值")

    # ════════════════════════════════════════════════
    # 元数据
    # ════════════════════════════════════════════════
    confidence = fields.Float(
        string="提取置信度", digits=(3, 2),
        help="AI 提取该元素的置信度（0.0 ~ 1.0）",
    )
    source_text = fields.Text(
        string="原文片段",
        help="从合同原文中截取的该元素所在上下文（用于追溯）",
    )
    is_required = fields.Boolean(
        string="必填", default=False,
        help="该元素在合同规范中是否必须存在",
    )

# ════════════════════════════════════════════════
# contract.config — 系统配置（单例）
# M17 · 配置域：编号规则 / 默认值 / 开关
# ════════════════════════════════════════════════
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ContractConfig(models.Model):
    """合同系统配置（单例模式）"""
    _name = "contract.config"
    _description = "合同系统配置"
    _inherit = ["mail.thread"]

    _sql_constraints = [
        ("single_row", "unique(id)", "配置表只允许一条记录"),
    ]

    # ════════════════════════════════════════════════
    # 编号规则
    # ════════════════════════════════════════════════
    code_prefix = fields.Char(
        string="编号前缀", default="CG-", required=True, tracking=True,
        help="合同编号前缀，如 CG- / HT- / 合同-",
    )
    code_date_format = fields.Selection(
        [
            ("none", "不包含日期"),
            ("YYMM", "两位年月（YYMM）"),
            ("YYYYMM", "四位年月（YYYYMM）"),
            ("YYYYMMDD", "完整日期（YYYYMMDD）"),
            ("year", "自然年占位符（%(year)s）"),
        ],
        string="日期格式", default="YYYYMM", tracking=True,
        help="编号中是否嵌入日期及格式",
    )
    code_padding = fields.Integer(
        string="流水号补零长度", default=4, required=True, tracking=True,
        help="流水号的补零位数，如 4 位 → 0001 / 5 位 → 00001",
    )
    code_start_no = fields.Integer(
        string="起始流水号", default=1, required=True,
        help="首次启用时的起始数字（已用 ir.sequence 管理实际计数，此字段仅作初始值）",
    )
    code_preview = fields.Char(
        string="编号预览", compute="_compute_code_preview",
        help="根据当前配置生成的样例编号",
    )

    # ════════════════════════════════════════════════
    # 默认值配置
    # ════════════════════════════════════════════════
    default_currency_id = fields.Many2one(
        "res.currency", string="默认币种",
        default=lambda self: self.env.company.currency_id,
    )
    default_payment_method = fields.Selection(
        [
            ("bank_transfer", "银行转账"),
            ("check", "支票"),
            ("cash", "现金"),
            ("letter_of_credit", "信用证"),
        ],
        string="默认结算方式", default="bank_transfer",
    )
    default_contract_type = fields.Selection(
        [
            ("purchase", "采购合同"),
            ("sale", "销售合同"),
            ("service", "服务合同"),
            ("lease", "租赁合同"),
        ],
        string="默认合同类型", default="purchase",
    )

    # ════════════════════════════════════════════════
    # 功能开关
    # ════════════════════════════════════════════════
    enable_ai_extract = fields.Boolean(
        string="启用 AI 自动提取", default=True,
        help="关闭后创建合同时不会自动调用 AI 服务",
    )
    enable_auto_payment_plan = fields.Boolean(
        string="自动生成收付款计划", default=False,
        help="保存合同后自动从付款条款解析收付款节点",
    )
    ai_service_url = fields.Char(
        string="AI 服务地址",
        default="http://ai-service:8000",
        help="AI 服务完整 URL，如 http://ai-service:8000 或 http://localhost:8000",
    )

    # ════════════════════════════════════════════════
    # 计算 / 单例
    # ════════════════════════════════════════════════
    @api.model
    def _get_config(self) -> "ContractConfig":
        """获取或创建唯一的配置记录"""
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = self.sudo().create({})
        return rec

    @api.depends("code_prefix", "code_date_format", "code_padding")
    def _compute_code_preview(self):
        from datetime import date
        today = date.today()
        date_part = self._format_date_part(today)
        seq_part = "1".zfill(self.code_padding or 4)
        self.code_preview = f"{self.code_prefix or ''}{date_part}{seq_part}"

    def _format_date_part(self, dt):
        fmt = self.code_date_format or "YYYYMM"
        mapping = {
            "none": "",
            "YYMM": dt.strftime("%y%m"),
            "YYYYMM": dt.strftime("%Y%m"),
            "YYYYMMDD": dt.strftime("%Y%m%d"),
            "year": dt.strftime("%Y"),
        }
        return mapping.get(fmt, "")

    # ════════════════════════════════════════════════
    # 序列生成：统一入口（contract.create() 调此方法）
    # ════════════════════════════════════════════════
    def generate_contract_code(self) -> str:
        """
        生成下一个合同编号。策略：
          1. 优先尝试 ir.sequence（Odoo 标准机制，原子安全）
          2. 如果 sequence 不存在，用 config 当前参数重建 sequence
          3. 兜底：自写文件锁计数器（极少见）
        """
        self.ensure_one()
        seq_code = "contract_ai.code"

        sequence = self.env["ir.sequence"].sudo().search(
            [("code", "=", seq_code)], limit=1
        )
        if not sequence:
            sequence = self._ensure_sequence(seq_code)

        # 用 Odoo 原生生成（它会用 prefix + %(year)s 等占位符）
        # 我们需要根据 config 重建 prefix
        self._sync_sequence(sequence)

        code = sequence.next_by_id()
        if not code or code == "/":
            # 兜底
            code = self._fallback_next_code()
        return code

    def _ensure_sequence(self, seq_code):
        """确保 ir.sequence 存在，不存在则创建"""
        return self.env["ir.sequence"].sudo().create({
            "name": "合同编号序列",
            "code": seq_code,
            "prefix": self._build_ir_sequence_prefix(),
            "number_next": self.code_start_no or 1,
            "number_increment": 1,
            "padding": self.code_padding or 4,
            "company_id": False,
        })

    def _sync_sequence(self, sequence):
        """将 config 参数同步到 ir.sequence"""
        prefix = self._build_ir_sequence_prefix()
        vals = {"prefix": prefix, "padding": self.code_padding or 4}
        sequence.sudo().write(vals)

    def _build_ir_sequence_prefix(self) -> str:
        """
        把 config 的日期格式映射成 ir.sequence 的占位符字符串。
        ir.sequence 原生支持：%(year)s / %(month)s / %(day)s
        """
        prefix = self.code_prefix or "CG-"
        fmt = self.code_date_format or "YYYYMM"
        date_placeholder = {
            "none": "",
            "YYMM": "%(year_y)s%(month)s-",     # 两位年需要自定义，fallback 用四位
            "YYYYMM": "%(year)s%(month)s-",
            "YYYYMMDD": "%(year)s%(month)s%(day)s-",
            "year": "%(year)s-",
        }
        return prefix + date_placeholder.get(fmt, "")

    def _fallback_next_code(self) -> str:
        """极端兜底：从 sequence 的 number_next 读一个"""
        seq = self.env["ir.sequence"].sudo().search(
            [("code", "=", "contract_ai.code")], limit=1
        )
        if seq:
            next_num = seq.number_next
            seq.sudo().write({"number_next": next_num + seq.number_increment})
            date_part = self._format_date_part(fields.Date.today())
            return f"{self.code_prefix or ''}{date_part}{str(int(next_num)).zfill(self.code_padding or 4)}"
        return f"{self.code_prefix or 'CG-'}{fields.Date.today().strftime('%Y%m')}0001"

    # ════════════════════════════════════════════════
    # Actions
    # ════════════════════════════════════════════════
    def action_save_and_apply(self):
        """保存配置并立即同步到 ir.sequence"""
        self.ensure_one()
        seq = self.env["ir.sequence"].sudo().search(
            [("code", "=", "contract_ai.code")], limit=1
        )
        if seq:
            self._sync_sequence(seq)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("配置已应用"),
                "message": _("合同编号规则已同步，预览: %s") % self.code_preview,
                "type": "success", "sticky": False,
            },
        }

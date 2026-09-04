# ════════════════════════════════════════════════
# contract.contract — 合同核心台账模型
# ════════════════════════════════════════════════
import base64
import json
import logging
import re
import requests
from datetime import date, timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class Contract(models.Model):
    """合同主台账"""
    _name = "contract.contract"
    _description = "合同台账"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    # ── SQL 约束：数据库层保证数据完整性 ──
    _sql_constraints = [
        # 合同编号唯一（允许为空，但一旦填写不能重复）
        ("code_uniq", "unique(code)", "合同编号不能重复"),
    ]

    # ════════════════════════════════════════════════
    # 基本信息
    # ════════════════════════════════════════════════
    name = fields.Char(
        string="合同名称", required=True, tracking=True,
        help="合同的完整名称，如「设备采购合同」",
    )
    code = fields.Char(
        string="合同编号", tracking=True, copy=False, index=True,
        help="合同唯一编号，如 CG-2025-001",
    )
    type = fields.Selection(
        [
            ("purchase", "采购合同"),
            ("sale", "销售合同"),
            ("service", "服务合同"),
            ("lease", "租赁合同"),
            ("labor", "劳动合同"),
            ("partnership", "合作协议"),
            ("consulting", "咨询合同"),
            ("other", "其他"),
        ],
        string="合同类型", tracking=True,
    )

    # ════════════════════════════════════════════════
    # 合同相对方（Many2one → contract.counterparty）
    # ════════════════════════════════════════════════
    partner_a = fields.Many2one(
        "contract.counterparty", string="甲方",
        ondelete="restrict", tracking=True,
        help="合同的甲方相对方",
    )
    partner_b = fields.Many2one(
        "contract.counterparty", string="乙方",
        ondelete="restrict", tracking=True,
        help="合同的乙方相对方",
    )
    signatory_id = fields.Many2one(
        "contract.signatory", string="签约方",
        ondelete="set null", tracking=True,
        help="我方签约人",
    )

    # ════════════════════════════════════════════════
    # 金额
    # ════════════════════════════════════════════════
    currency_id = fields.Many2one(
        "res.currency", string="币种",
        default=lambda self: self.env.company.currency_id,
    )
    amount = fields.Monetary(
        string="合同金额", currency_field="currency_id", tracking=True,
    )
    amount_uppercase = fields.Char(string="金额大写")

    # ════════════════════════════════════════════════
    # 日期
    # ════════════════════════════════════════════════
    date_signed = fields.Date(string="签订日期", tracking=True, index=True)
    date_start = fields.Date(string="生效日期")
    date_end = fields.Date(string="失效日期", index=True)

    # ════════════════════════════════════════════════
    # 业务条款
    # ════════════════════════════════════════════════
    payment_terms = fields.Text(string="付款方式")
    breach_clause = fields.Text(string="违约责任")
    dispute_resolution = fields.Selection(
        [
            ("litigation", "诉讼（法院管辖）"),
            ("arbitration", "仲裁（仲裁委员会）"),
            ("negotiation", "协商解决"),
        ],
        string="争议解决方式",
    )

    # ════════════════════════════════════════════════
    # 文件与 AI 数据
    # ════════════════════════════════════════════════
    source_pdf = fields.Binary(string="源文件 PDF")
    source_pdf_filename = fields.Char(string="文件名")
    ai_extracted_json = fields.Json(
        string="AI 提取原始 JSON",
        help="AI 服务返回的完整提取结果（调试/审计用）",
    )

    # ════════════════════════════════════════════════
    # 关联：付款计划 / 条款 / 元素
    # ════════════════════════════════════════════════
    payment_plan_ids = fields.One2many(
        "contract.payment.plan", "contract_id", string="收付款计划",
    )
    # ── M16 汇总计算字段 ──
    payment_plan_count = fields.Integer(
        string="计划数量", compute="_compute_payment_summary", store=True,
    )
    planned_amount_total = fields.Monetary(
        string="计划总额", currency_field="currency_id",
        compute="_compute_payment_summary", store=True,
        help="所有收付款计划的计划金额合计",
    )
    paid_amount_total = fields.Monetary(
        string="已完成金额", currency_field="currency_id",
        compute="_compute_payment_summary", store=True,
        help="状态为已完成的计划的实际金额合计",
    )
    remaining_amount_total = fields.Monetary(
        string="剩余待执行", currency_field="currency_id",
        compute="_compute_payment_summary", store=True,
        help="计划总额 − 已完成金额",
    )
    overdue_count = fields.Integer(
        string="逾期数", compute="_compute_payment_summary", store=True,
    )
    clause_ids = fields.One2many(
        "contract.clause", "contract_id", string="合同条款",
    )
    element_ids = fields.One2many(
        "contract.element", "contract_id", string="合同元素",
    )
    approval_log_ids = fields.One2many(
        "contract.approval.log", "contract_id", string="审批记录",
    )

    # ════════════════════════════════════════════════
    # 辅助字段
    # ════════════════════════════════════════════════
    classify_confidence = fields.Float(string="分类置信度", digits=(3, 2))
    extraction_confidence = fields.Float(string="提取置信度", digits=(3, 2))
    # ── 是否 AI 生成（computed：ai_extracted_json 有值即为 True）──
    is_ai_generated = fields.Boolean(
        string="AI 提取", compute="_compute_is_ai_generated",
        store=True, copy=False, index=True,
        help="字段是否由 AI 智能提取生成（基于 ai_extracted_json 是否为空）",
    )

    @api.depends("ai_extracted_json")
    def _compute_is_ai_generated(self):
        for rec in self:
            rec.is_ai_generated = bool(rec.ai_extracted_json)

    # ════════════════════════════════════════════════
    # 状态机
    # ════════════════════════════════════════════════
    state = fields.Selection(
        [
            ("draft", "草拟"),
            ("approval", "审批中"),
            ("seal", "已用印"),
            ("archived", "已归档"),
            ("void", "已作废"),
        ],
        string="状态", default="draft", tracking=True, copy=False, index=True,
        group_expand="_expand_states",
    )

    # ── 状态组展开（Kanban 按 state 分组时显示空列） ──
    @api.model
    def _expand_states(self, records, domain, context):
        return ["draft", "approval", "seal", "archived", "void"]

    # ════════════════════════════════════════════════
    # 自动生成合同编号（M17：优先用 config 模型）
    # ════════════════════════════════════════════════
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("code"):
                code = self._generate_code_via_config()
                if not code or code == "/":
                    # 兜底：Odoo 原生 ir.sequence
                    code = self.env["ir.sequence"].next_by_code("contract_ai.code") or "/"
                vals["code"] = code
        return super().create(vals_list)

    @api.model
    def _generate_code_via_config(self) -> str:
        """通过 contract.config 单例生成编号（M17 新机制）"""
        try:
            Config = self.env["contract.config"]
            config = Config._get_config()
            return config.generate_contract_code()
        except Exception:
            _logger.warning("config 编号生成失败，降级到原生 ir.sequence", exc_info=True)
            return ""

    # ════════════════════════════════════════════════
    # M17 · Excel 导出（跳转 HTTP 控制器）
    # ════════════════════════════════════════════════
    def action_export_excel(self):
        """导出当前合同列表为 Excel"""
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return {
            "type": "ir.actions.act_url",
            "url": f"{base_url}/contract/export/excel",
            "target": "self",
        }

    # ════════════════════════════════════════════════
    # 校验
    # ════════════════════════════════════════════════
    @api.constrains("date_start", "date_end")
    def _check_date_range(self):
        for rec in self:
            if rec.date_start and rec.date_end and rec.date_start > rec.date_end:
                raise ValidationError(_("生效日期不能晚于失效日期"))

    # ════════════════════════════════════════════════
    # AI 服务调用：一键提取
    # ════════════════════════════════════════════════
    def action_ai_extract(self):
        """调用 AI 服务：PDF → 分类 + 字段提取"""
        self.ensure_one()
        if not self.source_pdf:
            raise UserError(_("请先上传合同 PDF 文件"))

        ai_url = self._get_ai_service_url()
        pdf_bytes = base64.b64decode(self.source_pdf)

        try:
            files = {"file": (self.source_pdf_filename or "contract.pdf", pdf_bytes)}
            _logger.info("▶️ AI 提取开始 | 合同=%s | AI_URL=%s", self.name, ai_url)
            resp = requests.post(f"{ai_url}/api/contract/extract", files=files, timeout=60)
            _logger.info("   AI 响应 HTTP %s | body前200字=%s", resp.status_code, resp.text[:200])
            if resp.status_code != 200:
                raise UserError(_("AI 服务返回错误: %s") % resp.text)
            raw = resp.json()
        except requests.exceptions.ConnectionError:
            raise UserError(_(
                "无法连接 AI 服务 (%s)\n"
                "请检查 AI 服务是否启动或在 系统参数 中修改 contract_ai.ai_service_url"
            ) % ai_url)
        except Exception as e:
            raise UserError(_("AI 调用失败: %s") % str(e))

        # ⚠️ AI 服务返回统一包装 {"success": true, "data": {...}, "error": null}
        # 真正的提取/分类结果在 data 下面！
        if not raw.get("success"):
            raise UserError(_("AI 提取失败: %s") % raw.get("error", {}).get("message", "未知错误"))
        result = raw.get("data", raw)
        _logger.info("✅ 解包成功 | data keys=%s", list(result.keys()))

        self._apply_ai_result(result)

        # Odoo 17 没有 act_window_res（那是 18+ 才有的）。
        # 用 client reload + 重新打开 form action 实现刷新。
        return {
            "type": "ir.actions.act_window",
            "res_model": "contract.contract",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _get_ai_service_url(self):
        """从 Odoo 系统参数读取 AI 服务地址"""
        return self.env["ir.config_parameter"].sudo().get_param(
            "contract_ai.ai_service_url",
            "http://ai-service:8000",
        )

    def _apply_ai_result(self, result: dict):
        """将 AI 服务返回的 JSON 映射到模型字段

        AI 返回字段（已拍平，data.extraction 下）:
          contract_name, contract_code, partner_a, partner_b,
          amount, amount_uppercase, currency,
          sign_date, effective_date, expire_date,    ← AI 用下划线命名
          contract_type, payment_terms, breach_clause, dispute_resolution, confidence

        Odoo 模型字段:
          name, code, type, partner_a(M2O), partner_b(M2O),
          amount, amount_uppercase, currency_id(M2O),
          date_signed, date_start, date_end,          ← Odoo 风格
          payment_terms, breach_clause, dispute_resolution
        """
        extraction = result.get("extraction", {})
        classify = result.get("classify", {})

        _logger.info(
            "📋 _apply_ai_result 收到 | extraction keys=%s | classify=%s",
            list(extraction.keys()), classify,
        )

        # ── 中文合同类型 → Odoo Selection key ──
        ctype_map = {
            "采购合同": "purchase", "销售合同": "sale",
            "服务合同": "service", "租赁合同": "lease",
            "劳动合同": "labor", "合作协议": "partnership",
            "咨询合同": "consulting", "其他": "other",
        }

        # ── 1. 置信度 + 原始 JSON ──
        vals = {
            "ai_extracted_json": result,
            "classify_confidence": classify.get("confidence", 0),
            "extraction_confidence": extraction.get("confidence", 0),
        }

        # ── 2. 基本字段 ──
        if extraction.get("contract_name"):
            vals["name"] = extraction["contract_name"]
        if extraction.get("contract_code"):
            vals["code"] = extraction["contract_code"]
        if classify.get("contract_type"):
            vals["type"] = ctype_map.get(classify["contract_type"], False)

        # ── 3. 金额 ──
        if extraction.get("amount") is not None:
            vals["amount"] = extraction["amount"]
        if extraction.get("amount_uppercase"):
            vals["amount_uppercase"] = extraction["amount_uppercase"]

        # ── 4. 币种（CNY/USD → res.currency 查找）──
        currency_code = extraction.get("currency")
        if currency_code:
            currency = self.env["res.currency"].sudo().search(
                [("name", "=", currency_code)], limit=1,
            )
            if currency:
                vals["currency_id"] = currency.id

        # ── 5. 日期 ⚠️ 字段名必须和 AI 返回一致 ──
        #    AI 返回: sign_date / effective_date / expire_date
        #    Odoo 字段: date_signed / date_start / date_end
        vals["date_signed"] = self._parse_date(extraction.get("sign_date"))
        vals["date_start"] = self._parse_date(extraction.get("effective_date"))
        vals["date_end"] = self._parse_date(extraction.get("expire_date"))

        # ── 6. 业务条款 ──
        if extraction.get("payment_terms"):
            vals["payment_terms"] = extraction["payment_terms"]
        if extraction.get("breach_clause"):
            vals["breach_clause"] = extraction["breach_clause"]

        # ── 7. 争议解决（AI 返回长文本，用关键词 contains 匹配）──
        dispute_raw = str(extraction.get("dispute_resolution") or "")
        if dispute_raw:
            if "仲裁" in dispute_raw:
                vals["dispute_resolution"] = "arbitration"
            elif "法院" in dispute_raw or "诉讼" in dispute_raw or "起诉" in dispute_raw:
                vals["dispute_resolution"] = "litigation"
            elif "协商" in dispute_raw or "调解" in dispute_raw:
                vals["dispute_resolution"] = "negotiation"
            else:
                vals["dispute_resolution"] = "negotiation"

        _logger.info("📝 准备写入 vals: %s", {k: str(v)[:80] for k, v in vals.items()})
        self.write(vals)

        # ── 8. 甲乙方 Many2one 关联（AI 返回公司名 → 在 contract.counterparty 查找/创建）──
        self._link_counterparty("partner_a", extraction.get("partner_a"))
        self._link_counterparty("partner_b", extraction.get("partner_b"))

        # ── 9. 发布沟通记录 ──
        self.message_post(
            body=_("🤖 AI 自动提取完成 | 合同类型: %s | 置信度: %s")
                 % (classify.get("contract_type", "-"),
                    f"{classify.get('confidence', 0):.1%}"),
            subtype_id=self.env.ref("mail.mt_comment").id,
        )
        _logger.info(
            "✅ 合同 %s AI 提取完成 | name=%s | amount=%s | date_signed=%s",
            self.name, vals.get("name"), vals.get("amount"), vals.get("date_signed"),
        )

    def _link_counterparty(self, field_name: str, company_name):
        """
        根据 AI 返回的公司名，在 contract.counterparty 中查找或创建记录，
        然后绑定到当前合同的 Many2one 字段上。

        Args:
            field_name: "partner_a" 或 "partner_b"
            company_name: AI 提取的公司全称（如 "北京智慧城市建设发展有限公司"）
        """
        if not company_name:
            return
        company_name = str(company_name).strip()
        if not company_name:
            return

        Counterparty = self.env["contract.counterparty"].sudo()

        # 精确匹配已有记录
        cp = Counterparty.search([("name", "=", company_name)], limit=1)

        # 没找到 → 创建新的
        if not cp:
            cp = Counterparty.create({
                "name": company_name,
                "party_type": "company",
            })
            _logger.info("🆕 AI 提取新建 counterparty: name=%s id=%s", company_name, cp.id)

        # 绑定 Many2one
        self.write({field_name: cp.id})
        _logger.info("🔗 %s → %s (id=%s)", field_name, company_name, cp.id)

    @staticmethod
    def _parse_date(date_str):
        """兼容 YYYY-MM-DD / YYYY/MM/DD / 中文日期的日期解析"""
        if not date_str:
            return False
        if isinstance(date_str, date):
            return date_str
        # 中文日期 → ISO
        date_str = str(date_str).strip()
        for sep in ("-", "/", "年"):
            date_str = date_str.replace(sep, "-")
        date_str = date_str.replace("月", "-").replace("日", "")
        try:
            return date.fromisoformat(date_str)
        except (ValueError, TypeError):
            return False

    # ════════════════════════════════════════════════
    # M16 · 业财一体化：收付款计划自动生成 + 汇总
    # ════════════════════════════════════════════════

    @api.depends("payment_plan_ids", "payment_plan_ids.planned_amount",
                 "payment_plan_ids.actual_amount", "payment_plan_ids.state",
                 "payment_plan_ids.is_overdue")
    def _compute_payment_summary(self):
        for rec in self:
            plans = rec.payment_plan_ids
            rec.payment_plan_count = len(plans)
            rec.planned_amount_total = sum(plans.mapped("planned_amount"))
            rec.paid_amount_total = sum(
                p.actual_amount or 0 for p in plans if p.state == "paid"
            )
            rec.remaining_amount_total = rec.planned_amount_total - rec.paid_amount_total
            rec.overdue_count = sum(1 for p in plans if p.is_overdue)

    def generate_payment_plan(self):
        """
        解析 payment_terms 文本，自动生成收付款计划。

        解析策略（规则式，覆盖 80% 常见格式）：
          1. 按句切分（/[；;\n]）
          2. 每句提取：百分比 + 节点类型关键字 + 时间偏移
          3. 金额 = 合同总额 × 百分比；日期 = 签订日 + 偏移天数
          4. 无签订日时用今天；无明确节点时标记为 other
          5. 无明确时间时默认按节点序号等间隔排布

        典型格式示例：
          "合同签订后 5 个工作日内支付合同总金额的 30% 作为预付款"
          "设备到货并经验收合格后 10 个工作日内支付 60%"
          "剩余 10% 作为质保金，于质保期届满后 30 日内无息支付"
          "按季度结算，每季度末乙方提交服务报告"（ fallback 为等额分期）
        """
        self.ensure_one()
        if not self.amount or self.amount <= 0:
            raise UserError(_("合同金额为 0，无法生成收付款计划"))

        text = (self.payment_terms or "").strip()
        if not text:
            raise UserError(_("付款方式条款为空，请先填写"))

        # ── 清理旧计划（草稿或已取消的可以覆盖；已确认/已完成的要先处理）──
        if any(p.state in ("confirmed", "partial", "paid") for p in self.payment_plan_ids):
            raise UserError(_(
                "已存在确认/完成状态的收付款计划，请先处理或手动删除旧记录"
            ))
        self.payment_plan_ids.unlink()

        milestones = self._parse_payment_terms(text)

        if not milestones:
            # 兜底：生成一条 100% 尾款计划
            milestones = [{
                "name": "合同尾款",
                "milestone": "final",
                "percent": 1.0,
                "offset_days": 30,
                "raw_desc": text[:80],
            }]

        sign_date = self.date_signed or fields.Date.today()

        plan_vals = []
        for i, m in enumerate(milestones, 1):
            planned_amount = round(self.amount * m["percent"], 2)
            planned_date = sign_date + timedelta(days=m["offset_days"])
            plan_vals.append((0, 0, {
                "name": m["name"],
                "sort_order": i,
                "milestone": m["milestone"],
                "planned_amount": planned_amount,
                "planned_date": planned_date,
                "description": m.get("raw_desc", ""),
                "state": "draft",
            }))

        self.write({"payment_plan_ids": plan_vals})

        self.message_post(
            body=_("📋 自动生成收付款计划 | %d 个节点 | 合计 %.2f %s")
                 % (len(milestones), self.amount, self.currency_id.symbol),
            subtype_id=self.env.ref("mail.mt_comment").id,
        )
        _logger.info("合同 %s 自动生成 %d 个收付款节点", self.name, len(milestones))

        # 返回 act_window：前端重新拉取 record 数据，自动刷新 One2many 列表
        return {
            "type": "ir.actions.act_window",
            "res_model": "contract.contract",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _parse_payment_terms(self, text: str) -> list[dict]:
        """
        中文付款条款解析器。返回 list of dict:
          [{name, milestone, percent, offset_days, raw_desc}, ...]
        """
        # 按分号/换行切句
        sentences = [s.strip() for s in re.split(r"[；;\n]", text) if s.strip()]

        # ── 节点类型关键词 → milestone code ──
        milestone_map = [
            (r"预付", "prepayment", "预付款"),
            (r"[到送]货?.*(验|收|交)", "on_delivery", "到货款"),
            (r"[到送]货", "on_delivery", "到货款"),
            (r"(完工|竣工|完成).*(验|收|交)", "on_completion", "验收款"),
            (r"(验收|交(付|付?款)).*(合格|通过|完成)", "on_completion", "验收款"),
            (r"尾款|余款|质保金|质保期届满", "final", "尾款"),
            (r"分期|每月|按月|季度|按季", "installment", "分期款"),
        ]

        milestones = []
        total_percent = 0.0

        for sent in sentences:
            # 提取百分比：30% / 百分之30 / 30 ％
            pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%|百分之\s*(\d+(?:\.\d+)?)", sent)
            if not pct_match:
                continue
            percent = float(pct_match.group(1) or pct_match.group(2)) / 100.0

            # 提取固定金额（万元/元）
            fixed_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:万元|万元?|元)", sent)
            # 如果只有固定金额没百分比，跳过（按合同比例生成更好）

            # 确定节点类型
            milestone_code = "other"
            milestone_label = "其他"
            for pattern, code, label in milestone_map:
                if re.search(pattern, sent):
                    milestone_code = code
                    milestone_label = label
                    break

            # 提取时间偏移
            offset_days = self._parse_time_offset(sent, milestone_code)

            # 序号式命名
            seq = len(milestones) + 1
            name = f"第 {seq} 期 · {milestone_label}"

            milestones.append({
                "name": name,
                "milestone": milestone_code,
                "percent": percent,
                "offset_days": offset_days,
                "raw_desc": sent,
            })
            total_percent += percent

        # ── 百分比校验：总和偏离 100% 时标记 warning 但不阻止 ──
        if milestones:
            if abs(total_percent - 1.0) > 0.02:
                _logger.warning(
                    "合同 %s 付款条款解析：百分比合计 %.0f%%，偏离 100%% 较大，请人工核对",
                    self.name, total_percent * 100,
                )

        return milestones

    @staticmethod
    def _parse_time_offset(sent: str, milestone: str) -> int:
        """
        从一句中文里解析付款时间偏移天数（相对于签订日）。
        典型模式：
          "签订后 5 个工作日内" → +5
          "签订后" → +0
          "到货后 10 日内" → +30（到货日不确定时给合理估算）
          "月底/月初" → 不明确，给默认值
          无法解析 → fallback 按 milestone 给合理默认
        """
        # 显式天数：N 个工作日 / N 日内 / N 天内 / N 个月内
        day_match = re.search(
            r"(\d+)\s*个?\s*(?:工作日|日历日|日内|天内|日)"
            r"|(\d+)\s*个月?(?:内后)?",
            sent,
        )
        if day_match:
            if day_match.group(1):
                return int(day_match.group(1))
            elif day_match.group(2):
                return int(day_match.group(2)) * 30

        # 明确以签订日为基准
        if re.search(r"签订后|签约后|合同(签订|生效)后", sent):
            return 0

        # 明确以到货日为基准 → 估算 30 天（常见到货周期）
        if milestone == "on_delivery":
            return 30

        # 明确以验收/完工日为基准 → 估算 60 天
        if milestone == "on_completion":
            return 60

        # 尾款 → 默认 90 天
        if milestone == "final":
            return 90

        # 分期款 → 默认按月
        if milestone == "installment":
            return 30 * (len(re.findall(r"[，,、]", sent)) + 1) or 30

        # 都不明确 → 给一个安全默认
        return 15

    # ════════════════════════════════════════════════
    # 状态流转按钮（严格校验 + mail.thread 全记录）
    # ════════════════════════════════════════════════

    # ── 状态标签映射（用于 chatter 日志） ──
    _state_label = {
        "draft": "草拟",
        "approval": "审批中",
        "seal": "已用印",
        "archived": "已归档",
        "void": "已作废",
    }

    def _log_state_change(self, old_state, new_state):
        """辅助：统一的状态变更 chatter 日志"""
        self.message_post(
            body=_("状态变更：<b>%s</b> → <b>%s</b>") % (
                self._state_label.get(old_state, old_state),
                self._state_label.get(new_state, new_state),
            ),
            subtype_id=self.env.ref("mail.mt_comment").id,
        )

    # ════════════════════════════════════════════════
    # P0-2 · 审批流（对应肇新 Activiti 审批设计，Odoo 17 用
    #        ir.actions.server + contract.approval.log 实现）
    # 流转：draft --提交--> approval --通过--> seal --归档--> archived
    #                    └─驳回→ draft
    # ════════════════════════════════════════════════

    def _notify(self, title, message, msg_type="success"):
        """顶部通知（display_notification 客户端动作）"""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": msg_type},
        }

    def _log_approval(self, action: str, comment: str):
        """落一条审批日志（contract.approval.log）"""
        self.env["contract.approval.log"].create({
            "contract_id": self.id,
            "action": action,
            "comment": comment,
        })

    def action_submit_approval(self):
        """草拟 → 审批中（提交审批）"""
        for rec in self:
            if rec.state != "draft":
                return rec._notify("状态错误", "只有草拟状态才能提交审批", "warning")
            old = rec.state
            rec.state = "approval"
            rec._log_state_change(old, "approval")
            rec._log_approval("submit", "提交审批")
        return self._notify("已提交", "合同已提交审批")

    def action_approve(self):
        """审批中 → 已用印（审批通过）"""
        for rec in self:
            if rec.state != "approval":
                return rec._notify("状态错误", "非审批中状态", "warning")
            old = rec.state
            rec.state = "seal"
            rec._log_state_change(old, "seal")
            rec._log_approval("approve", "审批通过")
            rec.message_post(
                body=_("✅ 审批通过，进入用印阶段"),
                subtype_id=self.env.ref("mail.mt_comment").id,
            )
        return self._notify("已通过", "合同审批已通过")

    def action_reject(self):
        """审批中 → 草拟（审批驳回）"""
        for rec in self:
            if rec.state != "approval":
                return rec._notify("状态错误", "非审批中状态", "warning")
            old = rec.state
            rec.state = "draft"
            rec._log_state_change(old, "draft")
            rec._log_approval("reject", "审批驳回")
            rec.message_post(
                body=_("❌ 审批驳回，退回修改"),
                subtype_id=self.env.ref("mail.mt_comment").id,
            )
        return self._notify("已驳回", "合同已驳回", "danger")

    def action_archive(self):
        """已用印 → 已归档（严格：只有 seal 才能归档）"""
        for rec in self:
            if rec.state != "seal":
                raise UserError(_("只有已用印状态才能归档"))
            old = rec.state
            rec.state = "archived"
            rec._log_state_change(old, "archived")

    def action_void(self):
        """任意 → 已作废（不可逆）"""
        for rec in self:
            if rec.state == "void":
                continue
            old = rec.state
            rec.state = "void"
            rec._log_state_change(old, "void")

    def action_reset_draft(self):
        """审批中/已用印 → 草拟（归档/作废不可逆；审批中重置视为撤回）"""
        for rec in self:
            if rec.state in ("archived", "void"):
                raise UserError(_("%s 状态不可重置为草拟") % self._state_label.get(rec.state, rec.state))
            old = rec.state
            rec.state = "draft"
            rec._log_state_change(old, "draft")
            if old == "approval":
                rec._log_approval("cancel", "撤回审批，退回草拟")

    # ════════════════════════════════════════════════
    # P0-2 · Cron 定时任务（对照肇新 ContractJobService 定时扫描）
    # ════════════════════════════════════════════════

    @api.model
    def _cron_overdue_reminder(self):
        """收付款逾期提醒：扫描已逾期未完成的收付款计划，按合同聚合后在 chatter 提醒"""
        today = fields.Date.today()
        plans = self.env["contract.payment.plan"].search([
            ("planned_date", "<", today),
            ("state", "in", ("draft", "confirmed", "partial")),
            ("actual_date", "=", False),
        ])
        if not plans:
            return
        by_contract = {}
        for plan in plans:
            by_contract.setdefault(plan.contract_id, plan)
        for contract in by_contract:
            contract_plans = plans.filtered(lambda p: p.contract_id == contract)
            lines = "".join(
                "· %s（计划 %s，%.2f 元）<br/>" % (
                    p.name, p.planned_date, p.planned_amount,
                ) for p in contract_plans.sorted("planned_date")
            )
            contract.message_post(
                body=_("⚠️ <b>收付款逾期提醒</b>：有 %d 个节点已逾期未完成<br/>%s")
                     % (len(contract_plans), lines),
                subtype_id=self.env.ref("mail.mt_comment").id,
            )
        _logger.info("🔔 逾期提醒 cron：扫描 %d 个计划 / %d 份合同",
                     len(plans), len(by_contract))

    @api.model
    def _cron_expire_contracts(self):
        """合同到期自动归档：已用印且失效日期已过的合同自动流转到已归档"""
        today = fields.Date.today()
        expired = self.search([
            ("date_end", "<", today),
            ("state", "=", "seal"),
        ])
        if not expired:
            return
        for rec in expired:
            rec.state = "archived"
            rec.message_post(
                body=_("📦 合同已于 %s 到期，自动归档") % rec.date_end,
                subtype_id=self.env.ref("mail.mt_comment").id,
            )
        _logger.info("📦 到期归档 cron：自动归档 %d 份合同", len(expired))

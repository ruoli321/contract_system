# ═══════════════════════════════════════════════════════════════════
# M21 · 统一校验器（全系统唯一校验实现 — single source of truth）
#
# 定位（方案终版决策表）：
#   errors   硬错误   → 可重试：喂回 LLM 自我纠正（格式 / 幻觉证据 / 金额交叉 / sanity）
#   fatal    致命错误 → 不可重试：必填缺失或原文无信息源，重试无意义 → 直接转人工
#   warnings 警告     → 不重试：日期逻辑顺序 / 收付款计划合计不符 / 非关键证据缺失
#                       → 转 needs_review 标记人工
#
# 单一数据源：
#   critical 字段清单 = field_dict.json extract_schema.fields.*.critical
#   （extractor / contract_graph / gatekeeper 三处同源读取本模块导出的
#    CRITICAL_FIELDS，禁止再各自硬编码）
#
# 集成点：
#   extractor.py    内层重试循环：report.errors 非空 → 全量错误一次喂回
#   main.py         主链路：report + check_payment_schedule → gatekeeper 生成 quality
#   contract_graph  validate_node 调本模块（消除硬编码字段清单）
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from .chinese_amount import chinese_uppercase_to_amount
from .postprocess_fallback import (
    DATE_ANCHORS,
    DATE_ARABIC,
    DATE_CHINESE,
    AMOUNT_PATTERNS,
    AMOUNT_UPPERCASE,
    CODE_LABELLED,
    normalize_date,
)

logger = logging.getLogger("validator")


# ═══════════════════════════════════════════════════════════════════
# 配置（环境变量可覆盖，默认值经过 gold_contracts 校准）
# ═══════════════════════════════════════════════════════════════════

# 单份合同 LLM 阶段时间预算（秒）：超过即停止重试，降级转人工。
# Odoo 侧 AI_EXTRACT_TIMEOUT=300s，OCR 最长 ~60s，留 150s 给 LLM 阶段。
LLM_TIME_BUDGET = float(os.getenv("LLM_TIME_BUDGET_SECONDS", "150"))

# 公司名证据校验的相似度阈值（OCR 空格/全半角差异容错）
PARTNER_SIMILARITY_THRESHOLD = float(os.getenv("PARTNER_SIMILARITY_THRESHOLD", "0.90"))

# 金额 sanity 上限（100 亿元）
AMOUNT_SANITY_MAX = 1e10

# 收付款计划合计与合同金额的容差（1% 或 0.01 元取大）
SCHEDULE_TOLERANCE_RATIO = 0.01


# ═══════════════════════════════════════════════════════════════════
# 单一数据源：field_dict.json 的 critical 字段清单
# ═══════════════════════════════════════════════════════════════════

def _load_critical_fields() -> list[str]:
    candidates = [
        Path.cwd() / "prompts" / "field_dict.json",
        Path(__file__).resolve().parent.parent.parent.parent / "prompts" / "field_dict.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                fields = data.get("extract_schema", {}).get("fields", {})
                critical = [name for name, fdef in fields.items() if fdef.get("critical")]
                if critical:
                    return critical
            except Exception as e:
                logger.debug(f"读取 field_dict.json 失败 [{path}]: {e}")
    # field_dict.json 缺失时兜底（与 extractor 的 fallback 策略一致）
    logger.warning("field_dict.json 未找到，critical 字段使用内置兜底清单")
    return ["contract_name", "partner_a", "partner_b", "amount", "sign_date"]


CRITICAL_FIELDS = _load_critical_fields()

# 参与证据校验的短字段（长摘要字段 payment_terms/breach_clause 等不校验，
# 摘要允许改写，强行 substring 匹配会大量误报）
EVIDENCE_FIELDS = [
    "contract_name", "contract_code", "partner_a", "partner_b",
    "amount", "amount_uppercase",
    "sign_date", "effective_date", "expire_date", "currency",
]

# 原文信息源锚点：字段缺失时判断"原文有没有该信息"——
# 锚点存在 → 信息在但 LLM 没提出来（理论上可重试）；锚点不存在 → 原文缺，不可重试
SOURCE_ANCHORS = {
    "contract_code": re.compile(r"合同编号|协议编号|合同号|编号[：:]"),
    "partner_a": re.compile(r"甲方"),
    "partner_b": re.compile(r"乙方"),
    "amount": re.compile(r"金额|价款|总价|租金|费用|大写"),
    "amount_uppercase": re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{4,}"),
    "sign_date": re.compile(r"签订|签约|订立|日期"),
    "effective_date": re.compile(r"生效|起施行|自.{0,10}起"),
    "expire_date": re.compile(r"到期|有效期|截止|止于|届满"),
}


# ═══════════════════════════════════════════════════════════════════
# 校验报告
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ValidationReport:
    """validate_extraction 的结构化输出"""
    errors: list[str] = field(default_factory=list)           # 可重试硬错误
    fatal_errors: list[str] = field(default_factory=list)     # 不可重试
    warnings: list[str] = field(default_factory=list)         # 转 needs_review
    field_evidence: dict[str, bool] = field(default_factory=dict)
    critical_missing: list[str] = field(default_factory=list)
    system_confidence: float = 0.0
    is_fallback: bool = False

    @property
    def has_retryable_errors(self) -> bool:
        return bool(self.errors)

    def all_issues(self) -> list[str]:
        """全部问题（fatal + warnings + errors），gatekeeper 生成 review_reasons 用"""
        return [*self.fatal_errors, *self.warnings, *self.errors]

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════
# 归一化工具（OCR 空格 / 全半角 / 大小写差异容错）
# ═══════════════════════════════════════════════════════════════════

def norm_text(s) -> str:
    """NFKC 归一化 + 去全部空白 + 小写。用于原文 substring 类证据比对。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", "", s).lower()


_CURRENCY_SYMBOLS_FALLBACK = {
    "CNY": ["元", "人民币", "¥", "RMB"],
    "USD": ["美元", "$", "USD"],
    "EUR": ["欧元", "€", "EUR"],
    "GBP": ["英镑", "£", "GBP"],
    "HKD": ["港币", "HK$", "HKD"],
    "JPY": ["日元", "JPY"],
}


def _currency_symbols(code: str) -> list[str]:
    """币种代码 → 原文应出现的符号词列表（field_dict.currency.symbols 单一数据源）"""
    global _CURRENCY_SYMBOLS_MAP
    try:
        return _CURRENCY_SYMBOLS_MAP().get(code.upper(), [])
    except Exception:
        return _CURRENCY_SYMBOLS_FALLBACK.get(code.upper(), [])


def _CURRENCY_SYMBOLS_MAP() -> dict:
    global _SYMBOLS_CACHE
    if _SYMBOLS_CACHE is None:
        try:
            path = Path(__file__).resolve().parent.parent.parent.parent / "prompts" / "field_dict.json"
            if not path.exists():
                path = Path.cwd() / "prompts" / "field_dict.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            _SYMBOLS_CACHE = data.get("currency", {}).get("symbols", {}) or _CURRENCY_SYMBOLS_FALLBACK
        except Exception:
            _SYMBOLS_CACHE = _CURRENCY_SYMBOLS_FALLBACK
    return _SYMBOLS_CACHE


_SYMBOLS_CACHE: Optional[dict] = None


def collect_text_dates(text: str) -> set[str]:
    """收集原文全部日期并归一化为 YYYY-MM-DD 集合（日期证据比对用）"""
    found: set[str] = set()
    if not text:
        return found
    for m in DATE_ARABIC.finditer(text):
        d = normalize_date(m.group(0))
        if d:
            found.add(d)
    for m in DATE_CHINESE.finditer(text):
        d = normalize_date(m.group(0))
        if d:
            found.add(d)
    return found


def collect_amount_candidates(text: str) -> set[float]:
    """收集原文全部金额候选值（正则模式换算 + 大写换算），金额证据比对用"""
    candidates: set[float] = set()
    if not text:
        return candidates
    for pat, multiplier in AMOUNT_PATTERNS:
        for m in pat.finditer(text):
            try:
                candidates.add(round(float(m.group(1).replace(",", "")) * multiplier, 2))
            except (ValueError, IndexError):
                continue
    for m in AMOUNT_UPPERCASE.finditer(text):
        v = chinese_uppercase_to_amount(m.group(1))
        if v:
            candidates.add(round(v, 2))
    return candidates


# ═══════════════════════════════════════════════════════════════════
# 证据校验（短字段逐一核对"提取值是否真在原文中"——幻觉拦截）
# ═══════════════════════════════════════════════════════════════════

def _check_evidence(fname: str, value, text: str, norm_text_cache: str,
                    date_set: set[str], amount_set: set[float]) -> bool:
    """单字段证据校验。返回 True = 提取值能在原文中找到依据。"""
    if value is None or value == "":
        return True  # 缺失不算证据失败，缺失走 critical_missing 逻辑

    if fname == "amount":
        if not isinstance(value, (int, float)):
            return False
        return any(abs(value - c) < 0.01 for c in amount_set)

    if fname in ("sign_date", "effective_date", "expire_date"):
        return str(value) in date_set

    if fname == "amount_uppercase":
        # 提取值里的大写数字串必须出现在原文
        digits = re.sub(r"[^零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整角分]", "", str(value))
        return bool(digits) and digits in re.sub(r"\s+", "", text)

    if fname == "currency":
        # 币种反查：CNY → 原文应出现 元/人民币/¥/RMB 等符号（field_dict 单一数据源）
        symbols = _currency_symbols(str(value))
        return bool(symbols) and any(norm_text(s) and norm_text(s) in norm_text_cache for s in symbols)

    # contract_name / contract_code / partner_a / partner_b：子串匹配
    nv = norm_text(value)
    if not nv:
        return False
    if nv in norm_text_cache:
        return True
    # 公司名相似度兜底：OCR 断行/丢字时子串失败，但与原文某片段高度相似
    if fname in ("partner_a", "partner_b", "contract_name"):
        # 在原文中找最相似的等长窗口（按行切，避免全文滑窗 O(n²)）
        target_len = len(nv)
        best = 0.0
        for line in text.splitlines():
            nl = norm_text(line)
            if abs(len(nl) - target_len) > max(6, target_len // 2):
                continue
            for i in range(0, max(1, len(nl) - target_len + 1)):
                window = nl[i: i + target_len]
                if not window:
                    continue
                ratio = SequenceMatcher(None, nv, window).ratio()
                if ratio > best:
                    best = ratio
                if best >= PARTNER_SIMILARITY_THRESHOLD:
                    return True
        return best >= PARTNER_SIMILARITY_THRESHOLD
    return False


# ═══════════════════════════════════════════════════════════════════
# 主校验入口
# ═══════════════════════════════════════════════════════════════════

def validate_extraction(fields: dict, contract_text: str,
                        is_fallback: bool = False) -> ValidationReport:
    """
    提取结果全量校验（唯一校验入口）。

    Args:
        fields:        提取字段 dict（应已过 Pydantic + postprocess_fill）
        contract_text: 合同原文（证据比对）
        is_fallback:   是否为 LLM 全败后的正则抢救结果（强制低置信度）

    Returns:
        ValidationReport（errors 可重试 / fatal 不可重试 / warnings 转人工）
    """
    report = ValidationReport(is_fallback=is_fallback)
    text = contract_text or ""
    norm_cache = norm_text(text)
    date_set = collect_text_dates(text)
    amount_set = collect_amount_candidates(text)

    # ── 1. critical 字段缺失 → fatal（不可重试，直接转人工）──
    # 消息附带"原文是否有信息源"锚点判定，辅助人工定位问题
    for fname in CRITICAL_FIELDS:
        v = fields.get(fname)
        if v is None or v == "":
            report.critical_missing.append(fname)
            anchor = SOURCE_ANCHORS.get(fname)
            if anchor and anchor.search(text):
                report.fatal_errors.append(
                    f"业务关键字段缺失: {fname}（原文存在相关表述但未能提取，请人工补录）"
                )
            else:
                report.fatal_errors.append(
                    f"业务关键字段缺失: {fname}（原文中未找到相关信息源）"
                )

    # ── 2. 证据校验（幻觉拦截）──
    for fname in EVIDENCE_FIELDS:
        v = fields.get(fname)
        ok = _check_evidence(fname, v, text, norm_cache, date_set, amount_set)
        report.field_evidence[fname] = ok
        if v is not None and v != "" and not ok:
            # 有值但原文找不到 → LLM 抄写错误（幻觉），喂回重试有意义
            shown = str(v)[:40]
            report.errors.append(f"字段证据不符: {fname}='{shown}' 未能在原文中找到依据")

    # ── 3. 日期格式 + 值合法性（硬错误，触发重试）──
    for fname in ("sign_date", "effective_date", "expire_date"):
        v = fields.get(fname)
        if v is None:
            continue
        v = str(v)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            report.errors.append(f"{fname} 日期格式错误: '{v}'（应为 YYYY-MM-DD）")
            continue
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            report.errors.append(f"{fname} 日期值不合法: '{v}'")
        else:
            y = int(v[:4])
            if y < 1990 or y > 2100:
                report.errors.append(f"{fname} 年份越界: '{v}'（应在 1990-2100）")

    # ── 4. 日期逻辑顺序（警告：补签合同 effective<sign 合法，不触发重试）──
    sd, ed, xd = fields.get("sign_date"), fields.get("effective_date"), fields.get("expire_date")
    if sd and ed and str(sd) > str(ed):
        report.warnings.append(f"日期逻辑: 签订日期({sd}) 晚于 生效日期({ed})，可能为补签或提取错误")
    if ed and xd and str(ed) > str(xd):
        report.warnings.append(f"日期逻辑: 生效日期({ed}) 晚于 到期日期({xd})")

    # ── 5. 金额大写 ↔ 数字交叉（能解析且不一致 → 硬错误重试；解析失败跳过）──
    amount, upper = fields.get("amount"), fields.get("amount_uppercase")
    if amount is not None and upper:
        upper_num = chinese_uppercase_to_amount(upper)
        if upper_num is not None and abs(upper_num - float(amount)) >= 0.01:
            report.errors.append(
                f"金额交叉不一致: amount={amount} 与大写换算 {upper_num}（'{str(upper)[:30]}'）"
            )

    # ── 6. 金额 sanity（0/负数/超上限 → 硬错误重试）──
    if amount is not None:
        try:
            a = float(amount)
            if a <= 0:
                report.errors.append(f"金额异常: {amount}（≤0，疑似提取错误）")
            elif a > AMOUNT_SANITY_MAX:
                report.errors.append(f"金额异常: {amount}（超过 {AMOUNT_SANITY_MAX:.0e} 上限）")
        except (TypeError, ValueError):
            report.errors.append(f"金额类型异常: {amount!r}")

    # ── 7. 枚举合法性（dispute_resolution / currency / contract_type）──
    valid_dispute = {"向人民法院提起诉讼", "提交仲裁委员会仲裁", "协商解决", "调解解决"}
    if fields.get("dispute_resolution") and fields["dispute_resolution"] not in valid_dispute:
        report.errors.append(
            f"dispute_resolution 枚举非法: '{fields['dispute_resolution']}'"
        )

    # ── 8. 系统置信度（替代 LLM 自报 confidence）──
    report.system_confidence = _compute_system_confidence(report)

    if report.errors or report.fatal_errors or report.warnings:
        logger.info(
            f"🔍 validate_extraction | errors={len(report.errors)} "
            f"fatal={len(report.fatal_errors)} warnings={len(report.warnings)} | "
            f"system_confidence={report.system_confidence:.2f}"
        )
    return report


def _compute_system_confidence(report: ValidationReport) -> float:
    """
    系统置信度 = 关键字段完成度 ×0.35 + 证据通过率 ×0.35 + 无硬错误 ×0.30
    再按错误数量扣减。is_fallback 强制 ≤0.30。
    """
    n_critical = len(CRITICAL_FIELDS) or 1
    completeness = 1.0 - len(report.critical_missing) / n_critical

    # 证据通过率：只统计"有值参与校验"的字段
    evidence_ok = sum(1 for v in report.field_evidence.values() if v)
    evidence_total = len(report.field_evidence)
    evidence_rate = evidence_ok / evidence_total if evidence_total else 1.0

    base = 0.35 * completeness + 0.35 * evidence_rate + 0.30 * (0 if report.errors else 1)
    base -= 0.15 * len(report.errors) + 0.10 * len(report.fatal_errors) + 0.05 * len(report.warnings)

    conf = max(0.01, min(0.99, base))
    if report.is_fallback:
        conf = min(conf, 0.30)
    return round(conf, 3)


# ═══════════════════════════════════════════════════════════════════
# 收付款计划校验（业财一体化：分期合计 ≠ 合同金额 → 转人工）
# ═══════════════════════════════════════════════════════════════════

def check_payment_schedule(schedule: list[dict], amount) -> tuple[Optional[bool], Optional[str]]:
    """
    校验收付款计划节点合计 vs 合同金额。

    Returns:
        (ok, message)
        ok=True    合计一致
        ok=False   合计不一致（Odoo 侧据此禁止生成付款计划）
        ok=None    无法判定（无金额或无节点）
    """
    if not amount or not schedule:
        return None, None
    total = 0.0
    has_value = False
    for node in schedule:
        node_amount = node.get("amount")
        ratio = node.get("ratio")
        if node_amount:
            total += float(node_amount)
            has_value = True
        elif ratio:
            total += float(ratio) * float(amount)
            has_value = True
    if not has_value:
        return None, None
    tolerance = max(0.01, float(amount) * SCHEDULE_TOLERANCE_RATIO)
    if abs(total - float(amount)) <= tolerance:
        return True, None
    msg = (
        f"收付款计划合计 {round(total, 2)} ≠ 合同金额 {amount}"
        f"（容差 {round(tolerance, 2)}），计划未生成，请人工核对付款条款"
    )
    logger.warning(f"💰 schedule 校验失败: {msg}")
    return False, msg

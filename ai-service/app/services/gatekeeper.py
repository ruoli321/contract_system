# ═══════════════════════════════════════════════════════════════════
# M21 · 质检守门（gatekeeper）—— 响应顶层 quality 块的唯一生成方
#
# 职责：把 validator 报告 + 分类信号 + 解析质量 + 收付款计划校验
#       汇总为一个 quality 块，Odoo 侧据此决定：
#         - review_state = pending / no_need（人工审核闭环）
#         - critical_missing 字段留空不写
#         - payment_schedule_ok=False 时禁止生成付款计划
#         - pending 时禁止提交审批（Odoo action_submit_approval 卡点）
#
# 铁律：走完全流程仍不确定的问题，必须标记让人工处理，严禁静默放行。
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import os
from typing import Optional

from .validator import ValidationReport

logger = logging.getLogger("gatekeeper")

# ── 阈值（环境变量可调）──
# >= REVIEW_THRESHOLD 且无任何问题 → no_need（免审）
# STRICT_THRESHOLD ~ REVIEW_THRESHOLD → pending（有警告或置信度中等）
# < STRICT_THRESHOLD → pending + 严格标记（关键字段建议人工重录）
REVIEW_THRESHOLD = float(os.getenv("GATEKEEPER_REVIEW_THRESHOLD", "0.70"))
STRICT_THRESHOLD = float(os.getenv("GATEKEEPER_STRICT_THRESHOLD", "0.40"))
# M22: OCR 低置信块占比超过此值 → 单列一条强提醒（可靠性差）
LOW_CONF_RATIO_STRICT = float(os.getenv("GATEKEEPER_LOW_CONF_RATIO", "0.15"))


def build_quality(
    report: ValidationReport,
    schedule_ok: Optional[bool] = None,
    schedule_message: Optional[str] = None,
    classify_review_reasons: Optional[list[str]] = None,
    parse_quality: Optional[dict] = None,
) -> dict:
    """
    汇总生成 quality 块（/api/contract/extract 响应顶层字段）。

    Args:
        report:                   validator 校验报告
        schedule_ok:              收付款计划校验结果（True/False/None）
        schedule_message:         计划校验失败原因
        classify_review_reasons:  分类模块产生的待审原因（低置信/双通道冲突/强矛盾）
        parse_quality:            PDF 解析质量 {"score": float, "ok": bool, ...}

    Returns:
        quality dict
    """
    reasons: list[str] = []

    # ── 1. 校验器问题（errors 在重试耗尽后仍存在 → 全部转人工原因）──
    reasons.extend(f"校验失败: {e}" for e in report.fatal_errors)
    reasons.extend(f"警告: {w}" for w in report.warnings)
    reasons.extend(f"重试未解决: {e}" for e in report.errors)

    # ── 2. 收付款计划（业财红线）──
    payment_schedule_ok = schedule_ok
    if schedule_ok is False and schedule_message:
        reasons.append(f"业财校验: {schedule_message}")

    # ── 3. 分类信号 ──
    reasons.extend(classify_review_reasons or [])

    # ── 4. 解析质量守门 ──
    if parse_quality and parse_quality.get("ok") is False:
        reasons.append(
            f"解析质量差: score={parse_quality.get('score', 0):.2f}"
            f"（低于该类型阈值，OCR/文本层可能失真，请对照原件人工核对）"
        )

    # ── 4.5 M22: OCR 低置信靶点（识别结果与原件可能有出入的具体位置）──
    if parse_quality:
        low_blocks = parse_quality.get("low_conf_blocks") or []
        if low_blocks:
            total_blocks = parse_quality.get("ocr_total_blocks") or len(low_blocks)
            low_ratio = parse_quality.get("low_conf_ratio") or 0.0
            worst = min((b.get("score") or 1.0) for b in low_blocks)
            dropped_n = sum(1 for b in low_blocks if b.get("dropped"))
            pages = sorted({b.get("page") for b in low_blocks if b.get("page") is not None})
            msg = (
                f"OCR 识别 {len(low_blocks)}/{total_blocks} 块置信度不足"
                f"（最低 {worst:.2f}，页码 {pages}）"
            )
            if dropped_n:
                msg += f"，其中 {dropped_n} 块因置信度过低已被丢弃（内容缺失）"
            msg += "，请对照 PDF 原件定点核对"
            if low_ratio > LOW_CONF_RATIO_STRICT:
                reasons.append(f"OCR 识别可靠性差: {msg}")
            else:
                reasons.append(f"OCR 低置信靶点: {msg}")

    # ── 4.6 缺失页（救援未果/扫描页 OCR 失败）：内容整页缺失，红线信号 ──
    missing_pages = (parse_quality or {}).get("missing_pages") or []
    if missing_pages:
        reasons.append(
            f"第 {missing_pages} 页内容缺失（文本层损坏且 OCR 仲裁未果），"
            f"整页信息不在解析结果中，必须对照原件人工补录"
        )
    # 印章压字块（识别结果不可信区域，人工优先核对）
    seal_n = (parse_quality or {}).get("seal_overlapped_blocks") or 0
    if seal_n:
        reasons.append(
            f"检测到 {seal_n} 处印章压字文本（识别结果不可信），请对照原件核对盖章区域"
        )

    # ── 5. 置信度分层 ──
    system_confidence = report.system_confidence
    if report.is_fallback:
        reasons.append("LLM 全部尝试失败，结果为正则抢救值（可信度低）")
    if system_confidence < STRICT_THRESHOLD:
        reasons.append(
            f"系统置信度过低: {system_confidence:.2f}（<{STRICT_THRESHOLD}，关键字段请人工重录）"
        )
    elif system_confidence < REVIEW_THRESHOLD:
        reasons.append(f"系统置信度中等: {system_confidence:.2f}（<{REVIEW_THRESHOLD}，建议人工复核）")

    needs_review = bool(reasons)

    quality = {
        "needs_review": needs_review,
        "review_reasons": reasons,
        "critical_missing": report.critical_missing,
        "field_evidence": report.field_evidence,
        "system_confidence": system_confidence,
        "payment_schedule_ok": payment_schedule_ok,
        "is_fallback": report.is_fallback,
        "parse_quality": parse_quality,
        "thresholds": {
            "review": REVIEW_THRESHOLD,
            "strict": STRICT_THRESHOLD,
        },
    }

    # 结构化日志（上线监控：needs_review 比例 + 原因分布统计源）
    logger.info(
        "🛡️ gatekeeper | needs_review=%s | conf=%.2f | critical_missing=%s | reasons=%d条",
        needs_review, system_confidence, report.critical_missing, len(reasons),
    )
    for r in reasons:
        logger.info("   · %s", r)
    return quality

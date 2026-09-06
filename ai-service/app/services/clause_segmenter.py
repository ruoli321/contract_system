# ═══════════════════════════════════════════════════════════════════
# 合同条款分段 + 元素结构化（规则式，零 LLM 成本）
#
# segment_clauses(text):
#   按「第X条/第X章/1.」标题模式把合同全文切分为条款列表，
#   并按关键词推断条款类型（付款/违约/争议/保密/终止/质保/知产/不可抗力）。
#
# build_elements(extraction, text, clauses):
#   把 LLM 字段提取结果映射为 contract.element 结构（含分组、值类型、
#   原文片段定位、所属条款索引），供 Odoo 侧一键生成元素记录。
#
# 返回格式（Odoo contract_ai._apply_ai_result 消费）:
#   clauses:  [{sort_order, name, content, clause_type}]
#   elements: [{name, element_key, group, element_type,
#               value_text, value_number, value_date,
#               source_text, clause_index}]
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re

logger = logging.getLogger("clause-segmenter")

# ── 条款标题模式 ──
# 「第X条 / 第X款 / 第X章」：中文数字或阿拉伯数字
_CLAUSE_HEAD_RE = re.compile(
    r"^\s*(第\s*[零一二两三四五六七八九十百千0-9０-９]+\s*[条款章][、.．:：\s]*)"
)
# 「1. / 1、 / 1．」编号标题（仅当行短且编号后紧跟非数字时才认定）
_NUM_HEAD_RE = re.compile(r"^\s*(\d{1,3})\s*[、.．]\s*(?!\d)")

# 条款类型关键词（按特异性排序：先具体后通用）
_TYPE_RULES: list[tuple[str, str]] = [
    ("dispute", r"争议|纠纷|仲裁|诉讼|管辖|法院"),
    ("confidential", r"保密|商业秘密"),
    ("force_majeure", r"不可抗力"),
    ("intellectual_property", r"知识产权|著作权|专利|商标|版权"),
    ("warranty", r"质保|保修|质量标准|验收标准|售后服务"),
    ("termination", r"解除|终止|退出"),
    ("payment", r"付款|支付|价款|费用|结算|发票|押金|租金|定金|预付|报酬"),
    ("liability", r"违约|赔偿|责任"),
]

_TITLE_MAX_LEN = 30       # 标题行剩余长度上限（超过则视为正文）
_MAX_CLAUSES = 200        # 条款数上限（防御异常文本）
_SOURCE_WINDOW = 40       # 原文片段回溯/后延窗口字符数


def _guess_clause_type(title: str, content: str) -> str:
    """关键词推断条款类型；标题优先于正文"""
    for scope in (title, content):
        if not scope:
            continue
        for ctype, pattern in _TYPE_RULES:
            if re.search(pattern, scope):
                return ctype
    return "other"


def segment_clauses(text: str) -> list[dict]:
    """
    合同全文 → 条款列表（规则分段）

    规则:
      1. 以「第X条/第X章」开头的行开启新条款（OCR 逐行输出，天然行结构）
      2. 短行「1.」编号只在全文无「第X条」正式条款头时认作条款头
         （否则正文的 1./2. 子条目会被误拆）
      3. 其余行归入当前条款正文
      4. 首个条款头之前的文字（标题/甲乙方前导）不作为条款
      5. 全文无任何条款头时，返回单条「全文」兜底条款
    """
    if not text or not text.strip():
        return []

    clauses: list[dict] = []
    # cur = [head_label, title, content_lines]
    cur_head, cur_title, cur_lines = "", "", []
    formal_seen = False  # 是否已出现「第X条」正式条款头

    def _flush():
        nonlocal cur_head, cur_title, cur_lines
        content = "\n".join(cur_lines).strip()
        if cur_head and content:
            name = cur_title or cur_head.strip("、.．:： \t")
            clauses.append({
                "sort_order": len(clauses) + 1,
                "name": name[:50],
                "content": content,
                "clause_type": _guess_clause_type(cur_title, content),
            })
        cur_head, cur_title, cur_lines = "", "", []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _CLAUSE_HEAD_RE.match(line)
        if m:
            formal_seen = True
            _flush()
            head = m.group(1)
            rest = line[m.end():].strip()
            cur_head = head
            cur_title = rest if rest and len(rest) <= _TITLE_MAX_LEN else ""
            cur_lines = [line]
            continue
        num_m = _NUM_HEAD_RE.match(line) if not formal_seen else None
        if num_m and len(line) <= 60:
            _flush()
            cur_head = f"第{num_m.group(1)}条"
            rest = line[num_m.end():].strip()
            cur_title = rest if rest and len(rest) <= _TITLE_MAX_LEN else ""
            cur_lines = [line]
        else:
            # 未进入任何条款头的行（标题/前言）→ 丢弃
            if cur_head:
                cur_lines.append(line)

    _flush()

    if not clauses:
        # 兜底：无法识别条款结构 → 整篇作为一条
        clauses.append({
            "sort_order": 1,
            "name": "全文",
            "content": text.strip()[:5000],
            "clause_type": "other",
        })

    if len(clauses) > _MAX_CLAUSES:
        logger.warning("条款分段结果 %d 条超过上限，截断到 %d", len(clauses), _MAX_CLAUSES)
        clauses = clauses[:_MAX_CLAUSES]

    logger.info("📜 条款分段完成 | %d 条", len(clauses))
    return clauses


# ═══════════════════════════════════════════════════════════════════
# 元素结构化：LLM 提取字段 → contract.element 记录描述
# ═══════════════════════════════════════════════════════════════════

# (element_key, 中文名, group, element_type) —— 与 Odoo contract.element
# 的 group/element_type Selection 保持一致
_ELEMENT_SPECS: list[tuple[str, str, str, str]] = [
    ("contract_name", "合同名称", "basic", "text"),
    ("contract_code", "合同编号", "basic", "text"),
    ("contract_type", "合同类型", "basic", "selection"),
    ("partner_a", "甲方", "party", "text"),
    ("partner_b", "乙方", "party", "text"),
    ("amount", "合同金额", "amount", "number"),
    ("amount_uppercase", "金额大写", "amount", "text"),
    ("currency", "币种", "amount", "text"),
    ("sign_date", "签订日期", "date", "date"),
    ("effective_date", "生效日期", "date", "date"),
    ("expire_date", "失效日期", "date", "date"),
    ("payment_terms", "付款方式", "payment", "text"),
    ("breach_clause", "违约责任", "liability", "text"),
    ("dispute_resolution", "争议解决方式", "dispute", "text"),
]

# extract_data 里的非业务元数据 key（不生成元素）
_META_KEYS = {"confidence", "used_few_shot", "attempt_count",
              "elapsed_seconds", "payment_schedule", "amount_cross_check"}


def _find_source_text(value_str: str, text: str) -> str | None:
    """在原文中定位值的上下文片段（前后各取 _SOURCE_WINDOW 字符）"""
    if not value_str or not text:
        return None
    pos = text.find(value_str)
    if pos < 0 and len(value_str) > 12:
        pos = text.find(value_str[:12])
    if pos < 0:
        return None
    start = max(0, pos - _SOURCE_WINDOW)
    end = min(len(text), pos + len(value_str) + _SOURCE_WINDOW)
    return text[start:end].replace("\n", " ")


def _find_clause_index(value_str: str, clauses: list[dict]) -> int | None:
    """值出现在哪个条款正文里（1-based）；找不到返回 None"""
    if not value_str:
        return None
    for c in clauses:
        if value_str in c.get("content", ""):
            return c["sort_order"]
    return None


def build_elements(extraction: dict, text: str, clauses: list[dict]) -> list[dict]:
    """
    LLM 提取结果 → 元素列表

    - 只处理 _ELEMENT_SPECS 中定义的业务字段
    - 文本值尝试回溯原文片段（source_text）并定位所属条款（clause_index）
    - 金额写 value_number，日期写 value_date（ISO 字符串，Odoo 侧再解析）
    - value_text 恒写字符串化值，保证列表视图可直接显示
    """
    elements: list[dict] = []
    if not isinstance(extraction, dict):
        return elements

    confidence = extraction.get("confidence") or 0.8

    for key, label, group, etype in _ELEMENT_SPECS:
        value = extraction.get(key)
        if value is None or value == "":
            continue
        if key in _META_KEYS:
            continue

        el: dict = {
            "name": label,
            "element_key": key,
            "group": group,
            "element_type": etype,
            "value_text": str(value),
            "value_number": None,
            "value_date": None,
            "source_text": None,
            "clause_index": None,
            "confidence": confidence,
        }

        if etype == "number":
            try:
                el["value_number"] = float(value)
            except (TypeError, ValueError):
                el["element_type"] = "text"
        elif etype == "date":
            el["value_date"] = str(value)

        # 原文回溯 + 条款定位（仅对可搜索的短文本值有意义）
        searchable = el["value_text"] if len(el["value_text"]) <= 60 else None
        if searchable:
            el["source_text"] = _find_source_text(searchable, text)
            el["clause_index"] = _find_clause_index(searchable, clauses)

        elements.append(el)

    logger.info("🧩 元素结构化完成 | %d 个", len(elements))
    return elements

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
# 「一、二、三、」中文顿号章节编号（M24：仅当全文无「第X条」正式条款头时认作章节头，
# 避免第X条正文里的「一、二、」子列表被误拆）
_CN_HEAD_RE = re.compile(r"^\s*([一二三四五六七八九十]{1,3})\s*、\s*")
# 二级子项行（（一）/1./1、/(1)）——重排合并时不与前一行拼接
_SUBITEM_START_RE = re.compile(r"^\s*[（(][一二三四五六七八九十\d]{1,3}[)）]|^\s*\d{1,2}[、.．]")
# 键值行（合同编号：… / 甲方：…）——独立成行，不参与重排合并
_KV_LINE_RE = re.compile(r"^[^：:]{1,20}[：:]")
# 句末终止标点——上一行以此结尾时不与下一行合并
_TERMINAL_PUNCT_RE = re.compile(r"[。！？；]$")

# 条款类型关键词（按特异性排序：先具体后通用；main 兜底在 payment 之后）
_TYPE_RULES: list[tuple[str, str]] = [
    ("dispute", r"争议|纠纷|仲裁|诉讼|管辖|法院"),
    ("confidential", r"保密|商业秘密"),
    ("force_majeure", r"不可抗力"),
    ("intellectual_property", r"知识产权|著作权|专利|商标|版权"),
    ("warranty", r"质保|保修|质量标准|验收标准|售后服务"),
    ("termination", r"解除|终止|退出"),
    ("payment", r"付款|支付|价款|费用|结算|发票|押金|租金|定金|预付|报酬"),
    # M24 主条款兜底：金额/期限/生效/违约责任/权利义务等无专属类型的实体业务条款
    ("main", r"金额|期限|生效|标的|大写|违约|赔偿|责任|权利|义务|履行"),
]

_TITLE_MAX_LEN = 30       # 标题行剩余长度上限（超过则视为正文）
_MAX_CLAUSES = 200        # 条款数上限（防御异常文本）
_SOURCE_WINDOW = 40       # 原文片段回溯/后延窗口字符数

# CJK 字符与中文标点（用于文本规范化判断）
_CJK_CLASS = r"\u4e00-\u9fff\u3001\u3002\uff0c\uff1b\uff1a\uff1f\uff01\u201c\u201d\u2018\u2019\uff08\uff09"


def normalize_clause_text(text: str) -> str:
    """
    条款文本规范化（M24，保守规则，不碰业务数据）:
      - 全角空格/连续空白 → 单空格；行尾空白去掉
      - 汉字与中文标点之间的多余空格删除（PDF 逐行提取常见「标的为 智能产品」）
      - 含汉字的半角括号 → 全角括号
      - 汉字后半角逗号（后面不是数字）→ 全角逗号（千分位 320,000 不受影响）
    """
    if not text:
        return text
    t = text.replace("\u00a0", " ").replace("\u3000", " ")
    t = re.sub(r"[ \t]+", " ", t)
    # CJK（含中文标点）之间的空格删除
    t = re.sub(
        rf"(?<=[{_CJK_CLASS}]) +(?=[{_CJK_CLASS}])", "", t
    )
    # 含汉字的半角括号对 → 全角
    def _paren(m: re.Match) -> str:
        inner = m.group(1)
        return f"（{inner}）" if re.search(r"[\u4e00-\u9fff]", inner) else m.group(0)
    t = re.sub(r"\(([^()\n]{1,40})\)", _paren, t)
    # 汉字后半角逗号（后随非数字）→ 全角
    t = re.sub(rf"(?<=[{_CJK_CLASS}]),(?!\d)", "，", t)
    # 行尾空白 + 首尾整理
    t = "\n".join(ln.rstrip() for ln in t.splitlines()).strip()
    return t


def _reflow_lines(lines: list[str]) -> str:
    """
    段落回填合并（M24）：PDF 逐行输出会把一个句子切成多行，
    按启发式把「上一行未以句末标点结尾」的续行拼回同一段。

    保护规则（不合并）:
      - 下一行是二级子项行（（一）/1.）或键值行（含「：」头）→ 换行
      - 上一行以句末标点（。！？；）结尾 → 换行
      - 上一行以 ASCII 字母/数字/% 结尾（保护编号、ID、百分比）→ 换行
      - 上一行/当前行是键值行 → 换行
    """
    out: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out and not _is_kv(out[-1]) and not _is_kv(ln) \
                and not _SUBITEM_START_RE.match(ln) \
                and not _TERMINAL_PUNCT_RE.search(out[-1]) \
                and not re.search(r"[A-Za-z0-9%)％]$", out[-1]):
            out[-1] = out[-1] + ln
        else:
            out.append(ln)
    return "\n".join(out)


def _is_kv(line: str) -> bool:
    return bool(_KV_LINE_RE.match(line))


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
      2. 全文无「第X条」正式条款头时，「一、二、三、」中文编号行认作章节头
         （M24）；此时「1.」数字编号同样认作章节头（逻辑不变）
      3. 首个章节头之前的文字（标题/合同编号/甲乙方/前言）→ 「合同基本信息」
         条款，clause_type=other（M24：不再丢弃，用户要求头部信息入台账）
      4. 其余行归入当前章节正文，并做段落回填合并 + 文本规范化
      5. 全文无任何章节头时，返回单条「全文」兜底条款
    """
    if not text or not text.strip():
        return []

    clauses: list[dict] = []
    # cur = [head_label, title, content_lines]
    cur_head, cur_title, cur_lines = "", "", []
    pre_lines: list[str] = []  # 首个章节头之前的行（文头信息）
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
                "clause_type": _guess_clause_type(name, content),
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
        cn_m = _CN_HEAD_RE.match(line) if not formal_seen else None
        num_m = _NUM_HEAD_RE.match(line) if not formal_seen else None
        head_m = cn_m or num_m
        if head_m and len(line) <= 60:
            head_label = (cn_m.group(1) + "、") if cn_m else f"第{num_m.group(1)}条"
            _flush()
            rest = line[head_m.end():].strip()
            cur_head = head_label
            # 章节标题：编号后的短文本（一、合同标的 → 合同标的）
            cur_title = rest if rest and len(rest) <= _TITLE_MAX_LEN else ""
            cur_lines = [line]
        else:
            if cur_head:
                cur_lines.append(line)
            else:
                # 章节头之前的文头信息（标题/编号/甲乙方/前言）
                pre_lines.append(line)

    _flush()

    # M24：文头信息 → 首条「合同基本信息」（other），保证甲乙方等头部数据入台账
    if clauses and pre_lines:
        pre_content = _reflow_lines(pre_lines)
        if pre_content:
            clauses.insert(0, {
                "sort_order": 1,
                "name": "合同基本信息",
                "content": normalize_clause_text(pre_content),
                "clause_type": "other",
            })

    # 段落回填 + 规范化（header 条款已单独处理）
    # 注意：章节条款的第一行是标题行（如「一、合同标的」），须独立保留，
    # 只对正文行做回填，避免正文被拼进标题
    for c in clauses:
        if c.get("name") == "合同基本信息":
            continue
        body_lines = c["content"].splitlines()
        if not body_lines:
            continue
        head_line, body_lines = body_lines[0], body_lines[1:]
        body = _reflow_lines(body_lines)
        c["content"] = normalize_clause_text(
            (head_line + "\n" + body).strip() if body else head_line
        )

    if not clauses:
        # 兜底：无法识别条款结构 → 整篇作为一条
        clauses.append({
            "sort_order": 1,
            "name": "全文",
            "content": normalize_clause_text(text.strip()[:5000]),
            "clause_type": "other",
        })

    if len(clauses) > _MAX_CLAUSES:
        logger.warning("条款分段结果 %d 条超过上限，截断到 %d", len(clauses), len(clauses) - _MAX_CLAUSES)
        clauses = clauses[:_MAX_CLAUSES]

    # sort_order 统一重编号（插入 header 后保持 1..N 连续）
    for i, c in enumerate(clauses, start=1):
        c["sort_order"] = i

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

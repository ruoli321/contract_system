# ═══════════════════════════════════════════════════════════════════
# M7 · 文本清洗与语义切块
#
# 纯 Python 实现（仅依赖标准库 re / dataclasses），
# 用于把合同原始文本清洗后按语义切块，供后续 RAG 检索使用。
#
# 管线：
#   原始文本 → clean_text() 去噪 → chunk_text() 切块 → List[ContractChunk]
#
# 切块策略（三级优先级）：
#   1) 条款编号断点  ——  "第一条"/"1."/"一、"/"（一）" 等显式编号
#   2) 段落断点      ——  空行/缩进/大写标题
#   3) 硬长度约束    ——  每块 ≤ max_chars（默认 800，约 512 tokens）
#                       相邻块重叠 overlap（默认 50 字符）
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("chunker")


# ─────────────────────────────────────────────────────────────────────
# 数据类：切块结果
# ─────────────────────────────────────────────────────────────────────
@dataclass
class ContractChunk:
    """一个语义块"""
    chunk_id: str                    # 唯一标识：{source}:{index}
    text: str                        # 块文本
    index: int                       # 块序号（从 0 开始）
    char_count: int = 0              # 字符数
    clause_marker: Optional[str] = None   # 命中的条款编号（如 "第一条"/"3."），未命中为 None
    source_file: Optional[str] = None     # 来源 PDF 文件名
    page_num: Optional[int] = None        # 来源页码（如能拿到）
    metadata: dict = field(default_factory=dict)  # 扩展字段

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# 条款编号正则（覆盖中文 + 英文主流格式）
# ─────────────────────────────────────────────────────────────────────
# 条款标题优先级（从强到弱）：
_CLAUSE_PATTERNS = [
    # 所有 pattern 带 re.MULTILINE，让 ^ 匹配每行开头
    # 强 1: "第一条"/"第二条"/... "第X条"（中文数字 + 条/章/节/篇/部分）
    re.compile(r'^\s*(第[一二三四五六七八九十百千万〇零两\d]+[条章节部分篇])\s*[:：]?\s', re.MULTILINE),
    # 强 2: "1." / "2." / "12." 阿拉伯数字 + 点/顿号/右括号
    re.compile(r'^\s*(\d{1,3})[\.、)\)】]\s', re.MULTILINE),
    # 强 3: "一、"/"二、" 中文数字 + 顿号（顿号后可有可无空格）
    re.compile(r'^\s*([一二三四五六七八九十百千万〇零两]+)[、]\s*', re.MULTILINE),
    # 中: "（一）"/"(1)" 括号包裹（右括号后可有可无空格）
    re.compile(r'^\s*[（(]([一二三四五六七八九十百千万〇零两\d]+)[）)]\s*', re.MULTILINE),
    # 弱: 合同常见关键词标题（独立成行时才命中，避免误抓正文）
    re.compile(r'^\s*(争议解决|违约责任|付款方式|保密|不可抗力|合同生效|合同终止|特别约定|其他约定|定义|鉴于|甲方|乙方)\s*[:：]?\s*$', re.MULTILINE),
]


# ═══════════════════════════════════════════════════════════════════
# 第一阶段：文本清洗
# ═══════════════════════════════════════════════════════════════════

def clean_text(
    raw_text: str,
    *,
    filename: Optional[str] = None,
    remove_pagination: bool = True,
    remove_headers_footers: bool = True,
    verbose: bool = False,
) -> str:
    """
    清洗合同文本：去除噪声，保留语义结构。

    Args:
        raw_text:                原始文本（来自 pdfplumber 或 OCR）
        filename:                文件名（用于页码检测时的模式匹配）
        remove_pagination:       是否去除"第 N 页 / 共 M 页"
        remove_headers_footers:  是否去除页眉页脚
        verbose:                 是否打印清洗日志（参数化，避免污染主流程）

    Returns:
        清洗后的文本
    """
    if not raw_text:
        return ""

    steps_removed = []

    text = raw_text

    # ── Step 1: 统一换行符 ──
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # ── Step 2: 去除分页符 / OCR 噪声字符 ──
    # OCR 常见乱码：■●◆▲□△○◇ 等装饰符单独成行
    text = re.sub(r'^[\s\u2500-\u257F\u2580-\u259F\u25A0-\u25FF\u2600-\u26FF]+$', '', text, flags=re.MULTILINE)
    # 零宽字符 + 不间断空格
    text = text.replace('\u200b', '').replace('\u200c', '').replace('\u200d', '')
    text = text.replace('\xa0', ' ')

    # ── Step 3: 页码（多种格式）──
    if remove_pagination:
        before = len(text)
        # 行内或独立行："第 X 页 / 共 Y 页" / "Page X of Y"
        # 先处理独立行（整行只有页码）
        text = re.sub(r'(?:^|\n)\s*第\s*\d+\s*页\s*/\s*共\s*\d+\s*页\s*(?:\n|$)', '\n', text)
        text = re.sub(r'(?:^|\n)\s*Page\s+\d+\s+of\s+\d+\s*(?:\n|$)', '\n', text, flags=re.IGNORECASE)
        # 行内清理（同一行其他文字 + 页码）
        text = re.sub(r'第\s*\d+\s*页\s*/\s*共\s*\d+\s*页', '', text)
        text = re.sub(r'Page\s+\d+\s+of\s+\d+', '', text, flags=re.IGNORECASE)
        # "- 2 -" / "— X —" 独立行
        text = re.sub(r'(?:^|\n)\s*[—\-–]\s*\d{1,3}\s*[—\-–]\s*(?:\n|$)', '\n', text)
        # 孤立数字行（短且同行无其他内容）
        text = re.sub(r'(?:^|\n)\s*\d{1,3}\s*(?:\n|$)', '\n', text)
        removed = before - len(text)
        if removed > 0:
            steps_removed.append(f"页码 {removed} 字符")

    # ── Step 4: 页眉页脚 ──
    if remove_headers_footers and filename:
        # 如果有文件名，尝试匹配文件名出现在行首/行尾 → 页眉
        base = re.escape(filename.rsplit('.', 1)[0])
        before = len(text)
        text = re.sub(r'(?:^|\n)\s*' + base + r'[\s\-_]*\d*\s*(?:\n|$)', '\n', text, flags=re.IGNORECASE)
        removed = before - len(text)
        if removed > 0:
            steps_removed.append(f"页眉页脚 {removed} 字符")

    # ── Step 5: OCR 水印噪声 ──
    # "CONFIDENTIAL" / "机密" / "仅供内部使用" 等水印通常重复出现
    text = re.sub(r'(?i)confidential|internal\s+use\s+only|机密文件|仅供内部使用', '', text)

    # ── Step 6: 合并多余空行（≥3 连续空行压缩为 2 个）──
    text = re.sub(r'\n{3,}', '\n\n', text)

    # ── Step 7: 行首行尾空格 ──
    text = '\n'.join(line.strip() for line in text.split('\n'))

    # ── Step 8: 合并连续空格 ──
    text = re.sub(r'[ \t]{2,}', ' ', text)

    if verbose and steps_removed:
        logger.info(f"🧹 文本清洗: {', '.join(steps_removed)}")
    elif verbose:
        logger.info("🧹 文本清洗: 无噪声")

    return text.strip()


# ═══════════════════════════════════════════════════════════════════
# 第二阶段：条款编号检测
# ═══════════════════════════════════════════════════════════════════

def _find_clause_boundaries(text: str) -> list[tuple[int, int, str]]:
    """
    扫描全文，找出所有条款编号位置。

    Returns:
        List[(start_index, end_index, clause_marker_text)]
        按 start_index 升序排列。start_index 是编号文本的起始位置。
    """
    boundaries: list[tuple[int, int, str]] = []
    seen_ranges: list[tuple[int, int]] = []  # 去重：一个位置可能被多个 pattern 命中

    for pattern in _CLAUSE_PATTERNS:
        for m in pattern.finditer(text):
            start = m.start(1)
            end = m.end(1)
            marker = m.group(1).strip()

            # 去重：如果这个范围已被捕获，跳过
            overlap = any(abs(start - s) < 3 for (s, e) in seen_ranges)
            if not overlap:
                seen_ranges.append((start, end))
                boundaries.append((start, end, marker))

    # 按位置排序
    boundaries.sort(key=lambda x: x[0])
    return boundaries


# ═══════════════════════════════════════════════════════════════════
# 第三阶段：主切块函数
# ═══════════════════════════════════════════════════════════════════

def chunk_text(
    text: str,
    *,
    source_file: Optional[str] = None,
    max_chars: int = 800,
    overlap: int = 50,
    min_chars_per_chunk: int = 200,
    verbose: bool = False,
) -> list[ContractChunk]:
    """
    语义切块：优先条款编号 → 段落 → 硬长度。

    Args:
        text:                清洗后的文本（可 clean_text() 的返回值）
        source_file:         来源文件名（写入 chunk.metadata）
        max_chars:           单块最大字符数（默认 800 ≈ 512 tokens）
        overlap:             相邻块重叠字符数（默认 50，保留上下文）
        min_chars_per_chunk: 单块最小字符数（默认 200，避免太碎）
        verbose:             是否打印日志

    Returns:
        List[ContractChunk]  块列表，已按 index 排序
    """
    if not text:
        return []

    chunks: list[ContractChunk] = []

    # ── Step A: 找出所有条款编号断点 ──
    boundaries = _find_clause_boundaries(text)

    if verbose:
        logger.info(f"🔍 检测到 {len(boundaries)} 个条款编号断点")

    if boundaries:
        # ── 路径 1: 按条款编号切 ──
        raw_segments = _split_by_boundaries(text, boundaries)
    else:
        # ── 路径 2: 无显式编号，按段落空行切 ──
        raw_segments = _split_by_paragraphs(text)

    if verbose:
        logger.info(f"📦 初始分段: {len(raw_segments)} 段")

    # ── Step B: 段落聚合 + 硬长度约束 + 重叠 ──
    chunks = _aggregate_segments(
        raw_segments,
        source_file=source_file,
        max_chars=max_chars,
        overlap=overlap,
        min_chars=min_chars_per_chunk,
    )

    if verbose:
        logger.info(f"✅ 最终切块: {len(chunks)} 块 "
                     f"(avg {sum(c.char_count for c in chunks) // max(len(chunks),1)} chars)")

    return chunks


# ═══════════════════════════════════════════════════════════════════
# 内部：按条款编号切
# ═══════════════════════════════════════════════════════════════════

def _split_by_boundaries(
    text: str,
    boundaries: list[tuple[int, int, str]],
) -> list[dict]:
    """
    把文本按条款编号边界切成段落。

    Returns:
        List[{"text": str, "clause_marker": str|None}]
    """
    segments = []
    # 前置：开头到第一个编号之间（通常是合同前言/鉴于）
    first_boundary = boundaries[0][0] if boundaries else len(text)
    if first_boundary > 0:
        preamble = text[:first_boundary].strip()
        if preamble:
            segments.append({"text": preamble, "clause_marker": None})

    # 每个编号到下一个编号之间
    for i, (start, end, marker) in enumerate(boundaries):
        # 找到本段结束位置
        if i + 1 < len(boundaries):
            seg_end = boundaries[i + 1][0]
        else:
            seg_end = len(text)
        seg_text = text[start:seg_end].strip()
        if seg_text:
            segments.append({"text": seg_text, "clause_marker": marker})

    return segments


def _split_by_paragraphs(text: str) -> list[dict]:
    """
    无显式编号时，按空行 + 大写标题切段落。
    """
    # 先按空行切
    paragraphs = re.split(r'\n\s*\n', text.strip())
    result = []
    for p in paragraphs:
        p = p.strip()
        if len(p) < 10:
            continue  # 过短段落跳过
        result.append({"text": p, "clause_marker": None})
    return result


# ═══════════════════════════════════════════════════════════════════
# 内部：段落聚合 + 硬长度约束 + 重叠
# ═══════════════════════════════════════════════════════════════════

def _aggregate_segments(
    raw_segments: list[dict],
    *,
    source_file: Optional[str],
    max_chars: int,
    overlap: int,
    min_chars: int,
) -> list[ContractChunk]:
    """
    把初始段落聚合成最终块：
    - 段落本身超长 → 硬长度切
    - 多个短段落合并到 max_chars 以内（相邻块 overlap 保留上下文）
    - clause_marker 参考：块里第一个非 None 的 marker 会被记录
    - 允许不同编号的短段落合并（避免 min_chars 过滤掉所有块）
    """
    chunks: list[ContractChunk] = []
    index = 0

    # ── Step 1: 超长段落先硬切 ──
    expanded: list[dict] = []
    for seg in raw_segments:
        t = seg["text"]
        if len(t) <= max_chars:
            expanded.append(seg)
        else:
            sub_parts = _hard_split(t, max_chars, overlap)
            for sp in sub_parts:
                expanded.append({"text": sp, "clause_marker": seg["clause_marker"]})

    # ── Step 2: 聚合短段落（核心逻辑）──
    buffer_text = ""
    # 记录这个 buffer 里出现过的 clause_marker（取第一个非 None 的）
    buffer_first_marker: Optional[str] = None

    for seg in expanded:
        seg_text = seg["text"]
        seg_marker = seg["clause_marker"]

        # 记录第一个 clause_marker
        if buffer_first_marker is None and seg_marker is not None:
            buffer_first_marker = seg_marker

        # 长度约束：buffer + 新段 > max_chars 才 flush
        if len(buffer_text) + len(seg_text) + 1 > max_chars and buffer_text:
            chunks.append(_make_chunk(
                buffer_text, index, buffer_first_marker, source_file
            ))
            index += 1
            # 重叠 + 重置
            buffer_text = _get_overlap_tail(buffer_text, overlap)
            # 重叠部分的 marker 清空（新块重新标记）
            buffer_first_marker = None

        # 合并
        if buffer_text:
            buffer_text += "\n\n" + seg_text
        else:
            buffer_text = seg_text
            # buffer_first_marker 已在上面记录

    # 最后的 buffer
    if buffer_text:
        chunks.append(_make_chunk(
            buffer_text, index, buffer_first_marker, source_file
        ))

    # 过滤过短的孤立块（< min_chars 且没有 clause_marker）
    # 有 clause_marker 的块不过滤 — 它代表一个语义单元
    chunks = [
        c for c in chunks
        if c.char_count >= min_chars or c.clause_marker is not None
    ]

    # 兜底：如果全被过滤掉了（超短文本），保留第一个块，避免返回空列表
    if not chunks and raw_segments:
        fallback_text = raw_segments[0]["text"]
        chunks = [_make_chunk(
            fallback_text, 0,
            raw_segments[0].get("clause_marker"),
            source_file,
        )]

    # 重新编号
    for i, c in enumerate(chunks):
        c.index = i
        c.chunk_id = f"{source_file or 'unknown'}:{i:04d}"

    return chunks


def _make_chunk(
    text: str,
    index: int,
    clause_marker: Optional[str],
    source_file: Optional[str],
) -> ContractChunk:
    """构造一个 chunk"""
    return ContractChunk(
        chunk_id=f"{source_file or 'unknown'}:{index:04d}",
        text=text,
        index=index,
        char_count=len(text),
        clause_marker=clause_marker,
        source_file=source_file,
        metadata={
            "char_count": len(text),
            "has_clause_marker": clause_marker is not None,
        },
    )


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    """
    把超长文本硬切成若干子块。
    优先在句子边界切（.。！？\n），没有则在 max_chars 位置硬切。
    """
    if len(text) <= max_chars:
        return [text]

    parts = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))

        # 尝试在句子边界往前找
        if end < len(text):
            boundary_char = max(
                text.rfind('。', start, end),
                text.rfind('.', start, end),
                text.rfind('！', start, end),
                text.rfind('?', start, end),
                text.rfind('\n', start, end),
            )
            if boundary_char > start + max_chars // 2:  # 确保至少切半块
                end = boundary_char + 1  # 包含标点

        parts.append(text[start:end].strip())

        # 下一块起点 = 当前 end - overlap（重叠）
        start = end - overlap if end < len(text) else end

    return [p for p in parts if p]


def _get_overlap_tail(text: str, overlap: int) -> str:
    """取文本末尾 overlap 字符，作为下一块的重叠前缀"""
    if overlap <= 0:
        return ""
    if len(text) <= overlap:
        return text
    # 从 overlap 位置往前找最近的句子边界，避免截断半个词
    tail = text[-overlap * 2:]  # 多看一点
    last_boundary = max(
        tail.rfind('。'),
        tail.rfind('\n'),
    )
    if last_boundary >= 0 and last_boundary < len(tail) - 5:
        return tail[last_boundary + 1:]
    return text[-overlap:]


# ═══════════════════════════════════════════════════════════════════
# 便捷函数：clean + chunk 一行调用
# ═══════════════════════════════════════════════════════════════════

def process_contract_text(
    raw_text: str,
    *,
    source_file: Optional[str] = None,
    max_chars: int = 800,
    overlap: int = 50,
    verbose: bool = False,
    **clean_kwargs,
) -> list[ContractChunk]:
    """clean_text → chunk_text 一条龙"""
    cleaned = clean_text(raw_text, filename=source_file, verbose=verbose, **clean_kwargs)
    return chunk_text(
        cleaned,
        source_file=source_file,
        max_chars=max_chars,
        overlap=overlap,
        verbose=verbose,
    )

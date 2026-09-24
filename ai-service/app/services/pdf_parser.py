# ═══════════════════════════════════════════════════════════════════
# M6 · PDF 预处理与 OCR 分流
#
# 类 PdfProcessor：自动检测 PDF 类型 → 分流处理
#   - 页级分类：每页按"可抽取字符数 + 可读率 + 最大单图面积占比"
#     三信号独立判定 text / scanned / text_layered（可搜索扫描件）
#   - 纯文字版 → fitz 直接提取（乱码/低字符可疑页 OCR 仲裁兜底）
#   - 纯扫描版 → OpenCV 预处理（纠偏+去噪）→ PaddleOCR → 后处理
#   - 混合版   → 页级分流：文字页快抽 / 扫描页单独 OCR / 按页序拼回
#
# OCR 后处理链（2026-09 版面分析升级）：
#   ① 阅读顺序：按 rec_boxes 坐标行聚类排序（双栏/表格版面不再乱序）
#   ② 段落合并：行间距超阈值插入空行，还原段落结构
#   ③ 印章区块：HSV 红章检测 → 与印章重叠的文本块打 seal_overlap 标记
#   ④ 词汇纠错：合同领域错字词典，仅对低置信块应用（高置信不动）
#   ⑤ 置信度分流：低置信/被丢弃块 → quality.low_conf_blocks 人工靶点
#
# 返回值 PdfExtractionResult：结构化数据
#   text        — 合并后的纯文本
#   pages       — 每页信息（页码、文本、图片数、是否 OCR）
#   tables      — 识别到的表格（PP-Structure 结果）
#   pdf_type    — "text" / "scanned" / "mixed"（页级明细见 metadata）
#   ocr_engine  — 使用的 OCR 引擎
#   metadata    — 页数、处理耗时、引擎版本等
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import io
import re
import time
import threading
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("pdf-parser")


# ─────────────────────────────────────────────────────────────────────
# 数据类：结构化返回值
# ─────────────────────────────────────────────────────────────────────
@dataclass
class PageInfo:
    """单页信息"""
    page_num: int                     # 页码（从 1 开始）
    text: str                         # 该页文本
    is_ocr: bool = False              # 是否来自 OCR
    image_count: int = 0              # 该页图片数量
    text_layer_chars: int = 0         # 文本层字符数（用于判定）


@dataclass
class TableInfo:
    """识别到的表格"""
    page_num: int                     # 所在页码
    rows: list = field(default_factory=list)  # 二维数组 [row][col]
    html: str = ""                    # PP-Structure 的 HTML 输出


@dataclass
class PdfExtractionResult:
    """PDF 提取结果"""
    text: str = ""                    # 全文档纯文本（按页拼接）
    pages: list = field(default_factory=list)       # List[PageInfo]
    tables: list = field(default_factory=list)       # List[TableInfo]
    pdf_type: str = "unknown"         # "text" / "scanned" / "mixed"
    ocr_engine: Optional[str] = None  # 使用的 OCR 引擎名
    metadata: dict = field(default_factory=dict)    # 页数/耗时/版本等
    quality: Optional[dict] = None    # M21 解析质量评分（gatekeeper 守门用）

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容 dict"""
        return asdict(self)


@dataclass
class PageProbe:
    """单页三信号探测结果（2026-09：fitz 单次遍历同时产出，供分类+可疑仲裁）"""
    page_num: int              # 页码（1-based）
    chars: int                 # 文本层字符数（fitz get_text，含空白）
    readable_ratio: float      # 可读字符占比（乱码/私用区字符是损坏指纹）
    image_ratio: float         # 最大单图面积占比（扫描件 ≈1.0，签章 <0.15）
    image_area_sum: float      # 图片面积累加占比（观测用，不参与判定）


class EncryptedPdfError(ValueError):
    """PDF 已加密且无可用密码（main 层映射 400 PDF_ENCRYPTED，不再绕 OCR 兜底圈）"""


class OCREngineError(RuntimeError):
    """OCR 引擎不可用/整页渲染失败（main 层映射 500 OCR_UNAVAILABLE）

    扫描件没有 OCR 就没有内容，继续跑分类/提取毫无意义——旧行为返回
    "[OCR 引擎不可用]" 占位文本继续走分类链路，是垃圾进垃圾出漏洞。
    """


# ─────────────────────────────────────────────────────────────────────
# M21 · 解析质量评分（垃圾进垃圾出守门）
#
# 评分维度：可读字符率 + 乱码率。阈值按 pdf_type 分档——OCR 的有效
# 字符率天然低于文本层 PDF，单一阈值会对扫描件不公平。
# ─────────────────────────────────────────────────────────────────────

# 分档阈值：score 低于对应类型阈值 → quality.ok=False（转人工核对）
PARSE_QUALITY_THRESHOLDS = {
    "text": 0.85,         # 文本层 PDF：正常应接近满分
    "text_layered": 0.80, # 可搜索扫描件：隐藏文本层来自旧 OCR，空格错位/
                          # 错字天然拉低分数，单独分档防频繁误报
    "scanned": 0.70,      # OCR：允许更多噪声
    "mixed": 0.75,
    "unknown": 0.70,
}

# 乱码特征：控制字符（除 \n\r\t）、Unicode 私用区、替换符、
# CJK 兼容区怪字（pdfplumber ToUnicode 缺失的典型产物）
_GARBAGE_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ue000-\uf8ff\ufffd�]"
)
# 可读字符：汉字/字母/数字/常见中英文标点/空白
# （全角引号用 \u 转义，避免 ASCII 引号与字符串定界符混淆导致 raw 前缀失效；
#   \s 在 Unicode 模式下已覆盖全角空格 \u3000）
# 2026-09 补充合同高频符号：【】「」『』·〇①-⑩°——不补会把 OCR 表格页/
# 带【】条款页/大写日期"二〇二五"页的可读率无谓拉低，侵蚀乱码判定阈值信噪比
_READABLE_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3007\u2460-\u2469A-Za-z0-9"
    r"\u201c\u201d\u2018\u2019\u3010\u3011\u300c\u300d\u300e\u300f\u00b7\u00b0"
    r"，。、；：（）《》！？…—\s,.;:'()<>\[\]%￥¥$€/＆&@＠#＃*＊+＋=＝_＿|｜~～\\-]"
)


def compute_parse_quality(text: str, pdf_type: str) -> dict:
    """
    计算解析文本质量分（0-1）。

    score = 0.5 × 可读字符率 + 0.5 × (1 - 乱码率)
    ok    = score ≥ 对应 pdf_type 的阈值
    """
    if not text or not text.strip():
        return {"score": 0.0, "ok": False, "pdf_type": pdf_type,
                "readable_ratio": 0.0, "garbage_ratio": 1.0,
                "threshold": PARSE_QUALITY_THRESHOLDS.get(pdf_type, 0.70)}

    total = len(text)
    garbage = len(_GARBAGE_RE.findall(text))
    readable = len(_READABLE_RE.findall(text))
    readable_ratio = readable / total
    garbage_ratio = garbage / total
    score = round(max(0.0, min(1.0, 0.5 * readable_ratio + 0.5 * (1.0 - garbage_ratio))), 3)
    threshold = PARSE_QUALITY_THRESHOLDS.get(pdf_type, 0.70)

    return {
        "score": score,
        "ok": score >= threshold,
        "pdf_type": pdf_type,
        "readable_ratio": round(readable_ratio, 3),
        "garbage_ratio": round(garbage_ratio, 4),
        "threshold": threshold,
    }


# ─────────────────────────────────────────────────────────────────────
# M22 · OCR 块级置信度透传
#
# 原理：扫描件的原件是图像，机器没有"原文"可比——判断识别结果与原件
# 有没有出入，唯一来源是识别模型自评（rec_scores：模型对每个文本块的
# 解码概率，图像模糊/字形怪异 → 概率低 → 出入风险高）。
#
# 分级处理：
#   conf ≥ OCR_LOW_CONF_SCORE(0.85) → 正常采信
#   OCR_DROP_CONF_SCORE < conf < 0.85 → 进文本，但记为"低置信靶点"
#   conf ≤ 0.5 → 不进文本（内容缺失，风险更高！），记为 dropped 靶点
#
# 靶点汇总进 quality.low_conf_blocks（人工定点核对的依据），
# ratio 汇总为 low_conf_ratio 供 gatekeeper 决定是否转人工。
# ─────────────────────────────────────────────────────────────────────

OCR_LOW_CONF_SCORE = 0.85      # 低于此置信度的块 → 记录为低置信靶点
OCR_DROP_CONF_SCORE = 0.5      # 低于此置信度的块 → 不进文本（旧行为），标记 dropped
LOW_CONF_BLOCKS_MAX = 20       # quality 块中最多列出的靶点数（防响应膨胀）

# ─── 词汇纠错（2026-09）：合同领域高频 OCR 错字词典 ───
# 原则：
#   1. 只纠"错拼不成词"的单字误识（如"签定"在合同语境必为"签订"之误），
#      绝不碰法律上都是真词的对子——"定金/订金"担保属性不同，永不错改；
#   2. 仅对低置信块（conf < OCR_LOW_CONF_SCORE）应用，高置信识别结果不动；
#   3. 每处替换记入 quality.corrections 供人工抽查，可回溯。
CONTRACT_TYPO_DICT = {
    "签定": "签订",     # 签订合同
    "签定日": "签订日",
    "时问": "时间",     # bench 实测："签订时间"→"签订时问"
    "合间": "合同",
    "甲万": "甲方", "乙万": "乙方", "丙万": "丙方",
    "全额大写": "金额大写", "付软": "付款", "货软": "货款",
    "地止": "地址", "电活": "电话",
    "身份正": "身份证", "身分证": "身份证",
    "日其": "日期",
    "转帐": "转账", "帐号": "账号", "帐户": "账户",
    "峻工": "竣工", "骏工": "竣工",
    "履约保证全": "履约保证金",
    "增值悦": "增值税", "发累": "发票",
    "违药": "违约", "仲载": "仲裁",
}

# ─── 印章区块检测（2026-09 版面分析）───
# 原理：国内合同印章以红色为主，HSV 双区间覆盖正红/深红（H≈0±10 与 160-180），
# 形态学闭运算连接笔画 → 连通域 → 面积过滤（>0.3% 页面，排除散点噪声）。
# 与印章框重叠的 OCR 文本块打 seal_overlap 标记——印章压字区域的识别结果
# 置信度天然偏低且不可信，人工核对时优先看这些靶点。
SEAL_HSV_LOWER1 = (0, 60, 60)      # 正红 H 0-10
SEAL_HSV_UPPER1 = (10, 255, 255)
SEAL_HSV_LOWER2 = (160, 60, 60)    # 深红/品红 H 160-180
SEAL_HSV_UPPER2 = (180, 255, 255)
SEAL_MIN_AREA_RATIO = 0.003        # 印章最小面积占比（0.3% 页面，排除散点噪声）
# 文本块与印章的归属判定：OCR 块中心落入印章框即视为印章压字（_box_in_seal）


def merge_block_stats(acc: dict, blk: Optional[dict]) -> None:
    """把单页 OCR 块统计累加进累计器（就地修改）

    blk 结构（_paddleocr_run 产出）:
        total: 块总数；low_conf: 低置信/被丢弃靶点
        corrections: 词典纠错记录；seal_overlapped: 印章压字块数
    """
    if not blk:
        return
    acc["total"] = acc.get("total", 0) + blk.get("total", 0)
    acc.setdefault("low_conf", []).extend(blk.get("low_conf", []))
    if blk.get("corrections"):
        acc.setdefault("corrections", []).extend(blk["corrections"])
    acc["seal_overlapped"] = acc.get("seal_overlapped", 0) + blk.get("seal_overlapped", 0)


def attach_ocr_confidence(quality: dict, stats: Optional[dict]) -> dict:
    """
    把累计的 OCR 块级置信度统计合入 quality 块（M22）。

    Args:
        quality: compute_parse_quality 的返回值（就地修改）
        stats:   {"total", "low_conf", "corrections", "seal_overlapped"}
    """
    if not quality or not stats:
        return quality
    total = stats.get("total") or 0
    low = stats.get("low_conf") or []
    if total <= 0:
        return quality
    quality["ocr_total_blocks"] = total
    quality["low_conf_ratio"] = round(len(low) / total, 3)
    # 丢弃块风险最高排前面，其余按 score 升序
    low_sorted = sorted(low, key=lambda b: (not b.get("dropped"), b.get("score", 1.0)))
    quality["low_conf_blocks"] = low_sorted[:LOW_CONF_BLOCKS_MAX]
    # 词汇纠错回溯（人工抽查错改风险）
    if stats.get("corrections"):
        quality["corrections"] = stats["corrections"][:LOW_CONF_BLOCKS_MAX]
        quality["correction_count"] = len(stats["corrections"])
    # 印章压字块数（人工核对优先看这些区域）
    if stats.get("seal_overlapped"):
        quality["seal_overlapped_blocks"] = stats["seal_overlapped"]
    return quality


# ─────────────────────────────────────────────────────────────────────
# PdfProcessor — 主类
# ─────────────────────────────────────────────────────────────────────
class PdfProcessor:
    """
    PDF 智能处理器：自动分流文字版/扫描版

    用法:
        proc = PdfProcessor(ocr_engine="paddleocr")
        result = proc.extract_text(pdf_bytes)
        print(result.text)
        print(result.pdf_type)
    """

    # 扫描件判定阈值（经验值）
    SCANNED_TEXT_THRESHOLD = 50       # 清洗后字符数 < 此值视为扫描件（旧文档级判定，兜底用）
    MIXED_MIN_CHARS = 100             # > 此值判定为 mixed（旧文档级判定）

    # ── 页级判定（2026-09 升级：三信号，判定单位是"页"）──
    # 文档级累计字符会被少数文字页稀释（前几页 Word 封面 + 后面扫描图
    # 累计 ≥100 被误判 text，扫描页静默丢内容）；页级判定让每页各自达标。
    # 图片占比用"最大单图"而非累加：平铺背景（4×20%=80%）不再误判扫描，
    # 扫描仪半页拼接（2×50%）漏判时由可疑页仲裁兜底。
    PAGE_SCAN_CHARS = 50              # 单页可抽取字符 < 此值 → 需图片占比仲裁
    PAGE_FUZZY_MIN = 10               # 单页字符 < 此值 → 可疑（空/近空页）
    PAGE_RESCUE_CHARS = 30            # 单页字符 < 此值 → 可疑（矢量重绘无文本层候选）
    PAGE_READABLE_MIN = 0.5           # 可读率 < 此值 → 可疑（乱码页，全页检查，
                                      #  与字符数正交——大字符量私用区页同样要抓）
    PAGE_LAYERED_MIN_CHARS = 100      # text_layered 页字符 < 此值 → 可疑
                                      # （隐藏文本层常只有页眉页脚，正文在图里）
    PAGE_LAYERED_READABLE = 0.6       # text_layered 可读率 < 此值 → 可疑
                                      # （注意：抓不住"旧 OCR 错字"型失真——错字
                                      #  仍是可读汉字，由质量分+提取置信度兜底）
    PAGE_IMAGE_AREA_RATIO = 0.6       # 最大单图面积占比 ≥ 此值 → 扫描/图占页
                                      #（签章/LOGO 占比通常 <15%，扫描件整页图 ≈100%；
                                      #  分布双峰，0.6~0.8 区间几乎无真实页）

    def __init__(
        self,
        ocr_engine: str = "paddleocr",
        use_gpu: bool = False,
        lang: str = "ch",
        enable_table_detect: bool = True,
    ):
        """
        Args:
            ocr_engine:  OCR 引擎 ("paddleocr" | "pytesseract" | "none")
            use_gpu:     是否用 GPU（PaddleOCR only）
            lang:        OCR 语言 ("ch", "en", ...)
            enable_table_detect: 是否启用表格识别（PP-Structure）
        """
        self.ocr_engine = ocr_engine.lower()
        self.use_gpu = use_gpu
        self.lang = lang
        self.enable_table_detect = enable_table_detect

        # 延迟加载的引擎实例
        self._ocr = None
        self._table_engine = None
        self._ocr_initialized = False

        # PaddleOCR 实例非线程安全：端点已 asyncio.to_thread 化，并发请求
        # 在线程池跑 → 初始化与推理都必须持锁（文字 PDF 不走 OCR，零影响；
        # 扫描件并发时在锁上排队——这正是引擎要求的串行）
        self._ocr_lock = threading.Lock()

    # ═══════════════════════════════════════════════════════════════
    # 公开 API
    # ═══════════════════════════════════════════════════════════════

    def detect_type(self, pdf_bytes: bytes) -> str:
        """
        检测 PDF 类型（页级三信号判定聚合）

        Returns:
            "text"    — 全部页均为文字页（含 text_layered，走文本层抽取）
            "scanned" — 全部页均为扫描页
            "mixed"   — 文字页与扫描页混合（走页级分流管线）
            "unknown" — 检测失败
        """
        page_classes = self._classify_pages(pdf_bytes)
        if page_classes is None:
            return self._legacy_detect_type(pdf_bytes)
        if page_classes.count("scanned") == 0:
            return "text"
        has_text_side = ("text" in page_classes or "text_layered" in page_classes)
        return "mixed" if has_text_side else "scanned"

    # ───────────────────────────────────────────────────────────────
    # 页级分类（三信号：字符数 + 可读率 + 最大单图面积占比，fitz 单次遍历）
    # ───────────────────────────────────────────────────────────────

    def _probe_pages(self, pdf_bytes: bytes) -> Optional[list[PageProbe]]:
        """
        fitz 单次遍历产出每页三信号（分类 + 可疑仲裁共用一次探测）。

        为什么收敛到 fitz：旧实现 pypdf（字符）+ fitz（面积）双引擎探测、
        text 管线 pdfplumber 第三引擎抽取——同页三个引擎三个字符数，
        既双跑全文又埋"判定说有字、抽取抽不出"的漂移隐患。

        Returns:
            PageProbe 列表；fitz 不可用/打开/遍历失败返回 None；
            加密 PDF 抛 EncryptedPdfError（authenticate("") 成功的
            仅限 owner 密码加密件，正常继续处理）
        """
        try:
            import fitz
        except ImportError:
            logger.warning("PyMuPDF 未安装，三信号探测不可用")
            return None
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            logger.warning(f"fitz 打开失败: {e}")
            return None
        try:
            if doc.is_encrypted and not doc.authenticate(""):
                raise EncryptedPdfError("PDF 已加密且无可用密码，请先解密后上传")
            probes = []
            for i, page in enumerate(doc, start=1):
                text = page.get_text() or ""
                total = len(text)
                readable = len(_READABLE_RE.findall(text)) if total else 0
                page_area = abs(page.rect)
                max_single, area_sum = 0.0, 0.0
                if page_area > 0:
                    for img in page.get_images(full=True):
                        for rect in page.get_image_rects(img[0]):
                            a = abs(rect) / page_area
                            area_sum += a
                            if a > max_single:
                                max_single = a
                probes.append(PageProbe(
                    page_num=i,
                    chars=total,
                    readable_ratio=round(readable / total, 3) if total else 0.0,
                    image_ratio=round(min(max_single, 1.0), 3),
                    image_area_sum=round(min(area_sum, 1.0), 3),
                ))
            return probes
        except EncryptedPdfError:
            raise
        except Exception as e:
            logger.warning(f"页级三信号探测失败: {e}")
            return None
        finally:
            try:
                doc.close()
            except Exception:
                pass

    def _classes_from_probes(self, probes: list[PageProbe]) -> list[str]:
        """三信号 → 每页判定 text / scanned / text_layered"""
        classes = []
        for p in probes:
            if p.image_ratio >= self.PAGE_IMAGE_AREA_RATIO:
                # 大图占页：有可用文本层 → 可搜索扫描件（默认信文本层，可疑再仲裁）；
                # 无文本层 → 扫描页走 OCR
                classes.append(
                    "text_layered" if p.chars >= self.PAGE_SCAN_CHARS else "scanned"
                )
            else:
                classes.append("text")
        return classes

    def _classify_pages(self, pdf_bytes: bytes):
        """逐页分类，返回每页 "text" / "scanned" / "text_layered" 列表；探测失败返回 None"""
        try:
            probes = self._probe_pages(pdf_bytes)
        except EncryptedPdfError:
            raise
        except Exception as e:
            logger.warning(f"页级分类探测失败，退回文档级判定: {e}")
            return None
        if not probes:
            return None
        classes = self._classes_from_probes(probes)
        logger.debug(f"  页级分类: {classes}")
        return classes

    def _page_is_suspect(self, probe: PageProbe, page_class: str) -> bool:
        """
        可疑页仲裁判定（触发 → 单独渲染 OCR，结果替换文本层）

        乱码信号（readable_ratio）与字符数分支正交、全页检查——ToUnicode
        损坏页可能抽出几百个私用区字符，只看 chars 阈值会漏（10~49 字符
        乱码页曾是静默丢内容的盲区）。
        """
        if probe.chars < self.PAGE_FUZZY_MIN:             # 空/近空页
            return True
        if probe.readable_ratio < self.PAGE_READABLE_MIN:  # 乱码页（任意字符量）
            return True
        if page_class == "text_layered":
            # 隐藏文本层疑似失真：稀疏（常只有页眉页脚，正文在图里）或乱码
            return (probe.chars < self.PAGE_LAYERED_MIN_CHARS
                    or probe.readable_ratio < self.PAGE_LAYERED_READABLE)
        if probe.chars < self.PAGE_RESCUE_CHARS:           # 低字符矢量重绘候选
            return True
        return False

    def _legacy_detect_type(self, pdf_bytes: bytes) -> str:
        """旧文档级判定（页级分类不可用时的兜底）"""
        try:
            has_text, total_chars = self._has_text_layer(pdf_bytes)

            if not has_text:
                return "scanned"
            elif total_chars < self.SCANNED_TEXT_THRESHOLD:
                return "scanned"
            elif total_chars < self.MIXED_MIN_CHARS:
                return "mixed"
            else:
                return "text"
        except Exception as e:
            logger.error(f"detect_type 失败: {e}")
            return "unknown"

    def extract_text(self, pdf_bytes: bytes) -> PdfExtractionResult:
        """
        主入口：三信号判定 → 分流处理 → 后处理 → 质量评分

        Raises:
            EncryptedPdfError: PDF 加密且无可用密码
            OCREngineError:   扫描件管线 OCR 引擎不可用/渲染失败
        """
        t0 = time.time()

        # M22: OCR 块级置信度累计器——局部变量随调用栈传递。实例被全局复用，
        # 旧实现放实例属性且每次重置，线程池并发下请求会互相清零/混串
        ocr_stats: dict = {}

        # 1) 页级三信号探测 + 分类（判定单位是"页"）
        probes = self._probe_pages(pdf_bytes)
        if probes is None:
            page_classes = None
            pdf_type = self._legacy_detect_type(pdf_bytes)
            logger.info(f"📄 PDF 类型检测: {pdf_type}（三信号探测不可用，退回文档级）")
        else:
            page_classes = self._classes_from_probes(probes)
            n_text = page_classes.count("text")
            n_layered = page_classes.count("text_layered")
            n_scan = page_classes.count("scanned")
            pdf_type = ("text" if n_scan == 0
                        else "scanned" if (n_text + n_layered) == 0 else "mixed")
            logger.info(
                f"📄 PDF 类型检测: {pdf_type} "
                f"（页级: {n_text} 文字页 / {n_layered} 可搜索扫描页 / "
                f"{n_scan} 扫描页 / 共 {len(page_classes)} 页）"
            )

        # 2) 分流
        if pdf_type == "text":
            # text_layered 页也在文本层管线处理（可疑页仲裁兜底）
            result = self._text_pipeline(
                pdf_bytes, probes=probes, page_classes=page_classes,
                ocr_stats=ocr_stats,
            )
        elif pdf_type == "scanned":
            result = self._ocr_pipeline(pdf_bytes, ocr_stats=ocr_stats)
        elif pdf_type == "mixed":
            # 页级分流：文字页快速抽取 / 扫描页单独 OCR / 按页序拼回
            result = self._hybrid_pipeline(
                pdf_bytes, page_classes, probes=probes, ocr_stats=ocr_stats
            )
        else:
            # 三信号与文档级判定都失败
            result = PdfExtractionResult(
                text="", pdf_type="unknown", metadata={"error": "检测失败"}
            )
        if page_classes:
            result.metadata["page_classes"] = page_classes
        if probes:
            result.metadata["page_probes"] = [asdict(p) for p in probes]

        # 2.5) NUL(0x00) 清洗：pdfplumber/pypdf 解析部分 PDF（嵌入字体
        #      ToUnicode 映射缺失等）时文本层会混入 \x00。Python/JSON 层面
        #      均合法所以上游无感，但 PostgreSQL TEXT 类型拒绝含 NUL 的
        #      字符串，不清洗会导致 Odoo 侧写库失败
        if "\x00" in result.text:
            logger.warning(
                "⚠️ 检测到 NUL(0x00) 字符 %d 处，已清洗", result.text.count("\x00")
            )
            result.text = result.text.replace("\x00", "")
            for p in result.pages:
                p.text = p.text.replace("\x00", "")

        # 3) 记录耗时
        result.metadata["elapsed_seconds"] = round(time.time() - t0, 3)
        result.metadata["page_count"] = len(result.pages)
        result.metadata["ocr_engine"] = self.ocr_engine

        # 4) M21 解析质量评分（垃圾进垃圾出守门：质量差 → 上游转人工）
        # 全 text 型但含 text_layered 页 → 用独立分档（隐藏文本层来自旧 OCR，
        # 空格错位/错字天然拉低分数，沿用 0.85 会频繁误报）
        quality_type = result.pdf_type
        if (quality_type == "text" and page_classes
                and "text_layered" in page_classes):
            quality_type = "text_layered"
        result.quality = compute_parse_quality(result.text, quality_type)

        # 4.5) M22: OCR 块级置信度透传——低置信/被丢弃块 → 人工核对靶点
        if ocr_stats.get("total"):
            attach_ocr_confidence(result.quality, ocr_stats)
            low_n = len(result.quality.get("low_conf_blocks") or [])
            if low_n:
                logger.warning(
                    "⚠️ OCR 低置信靶点 %d 处 / 共 %d 块（已写入 quality.low_conf_blocks）",
                    low_n, result.quality.get("ocr_total_blocks", 0),
                )
            corr_n = result.quality.get("correction_count", 0)
            if corr_n:
                logger.info(
                    "✏️ 词典纠错 %d 处（已写入 quality.corrections）", corr_n
                )

        # 4.6) 缺失页信号：救援未果/扫描页 OCR 失败的页 → gatekeeper 强制转人工
        missing = result.metadata.get("missing_pages") or []
        if missing and result.quality is not None:
            result.quality["missing_pages"] = missing
            result.quality["ok"] = False
            logger.warning("⚠️ 第 %s 页内容缺失（已写入 quality.missing_pages）", missing)

        logger.info(
            f"✅ 提取完成: {len(result.text)} 字符, "
            f"{len(result.pages)} 页, {len(result.tables)} 表格, "
            f"耗时 {result.metadata['elapsed_seconds']}s"
        )
        return result

    # ═══════════════════════════════════════════════════════════════
    # 类型检测底层：检测 PDF 是否有文本层
    # ═══════════════════════════════════════════════════════════════

    def _has_text_layer(self, pdf_bytes: bytes) -> tuple[bool, int]:
        """
        用 pypdf 检测 PDF 是否有文本层

        Returns:
            (has_text_layer, total_chars)
        """
        total_chars = 0
        page_count = 0
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_bytes))
            page_count = len(reader.pages)
            for page in reader.pages:
                text = page.extract_text() or ""
                total_chars += len(text)

            # pypdf 本身不报错但可能返回空 → 再用 pdfplumber 交叉验证
            if total_chars < 10:
                try:
                    import pdfplumber
                    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                        for page in pdf.pages:
                            total_chars += len(page.extract_text() or "")
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"pypdf 检测文本层失败: {e}")
            return False, 0

        # 判定
        has_text = total_chars > self.SCANNED_TEXT_THRESHOLD
        logger.debug(
            f"  文本层检测: {page_count} 页, {total_chars} 字符 → "
            f"{'有文本层' if has_text else '无文本层'}"
        )
        return has_text, total_chars

    # ═══════════════════════════════════════════════════════════════
    # 管线 A：文字版 PDF
    # ═══════════════════════════════════════════════════════════════

    def _text_pipeline(self, pdf_bytes: bytes, probes=None,
                       page_classes=None,
                       ocr_stats: Optional[dict] = None) -> PdfExtractionResult:
        """
        文本层抽取管线：fitz 主抽取（与三信号探测同引擎，字符数零漂移），
        pdfplumber→pypdf 降级兜底；可疑页（乱码/低字符/text_layered 失真）
        单独渲染 OCR 仲裁替换。
        """
        if ocr_stats is None:
            ocr_stats = {}
        pages: list[PageInfo] = []
        pipeline_name = "fitz"
        try:
            import fitz
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                for i, page in enumerate(doc, start=1):
                    text = page.get_text() or ""
                    pages.append(PageInfo(
                        page_num=i,
                        text=text,
                        is_ocr=False,
                        image_count=len(page.get_images() or []),
                        text_layer_chars=len(text),
                    ))
        except Exception as e:
            # ImportError(未安装) 与抽取失败统一走 pdfplumber→pypdf 兜底
            logger.warning(f"fitz 抽取不可用（{e}），pdfplumber 兜底")
            pipeline_name = "pdfplumber"
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    for i, page in enumerate(pdf.pages):
                        text = page.extract_text() or ""
                        pages.append(PageInfo(
                            page_num=i + 1,
                            text=text,
                            is_ocr=False,
                            image_count=len(page.images or []),
                            text_layer_chars=len(text),
                        ))
            except Exception as e2:
                logger.warning(f"pdfplumber 不可用（{e2}），pypdf 兜底")
                pipeline_name = "pypdf"
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(io.BytesIO(pdf_bytes))
                    for i, page in enumerate(reader.pages):
                        text = page.extract_text() or ""
                        try:
                            img_count = len(page.images)
                        except Exception:
                            img_count = 0
                        pages.append(PageInfo(
                            page_num=i + 1, text=text, is_ocr=False,
                            image_count=img_count, text_layer_chars=len(text),
                        ))
                except Exception as e3:
                    logger.error(f"pypdf 兜底也失败: {e3}")

        # ── 可疑页仲裁（页级判定的最后兜底）：乱码页（全页可读率<0.5）、
        # 低字符页、text_layered 失真页 → 单独渲染送 OCR 替换文本层。
        # 探测不可用（降级链抽取）时退回旧判定：单页字符 <10
        if probes and page_classes:
            suspect = [p.page_num for p, c in zip(probes, page_classes)
                       if c != "scanned" and self._page_is_suspect(p, c)]
        else:
            suspect = [p.page_num for p in pages
                       if p.text_layer_chars < self.PAGE_FUZZY_MIN]
        rescued: list = []
        unrescued: list = []
        if suspect:
            logger.info(
                f"🔎 可疑页仲裁: 第 {suspect} 页文本层异常（乱码/低字符/失真），OCR 补齐"
            )
            if self._init_ocr():
                try:
                    images = self._pdf_to_images(pdf_bytes, page_nums=set(suspect))
                    for page_num, img in zip(suspect, images):
                        pre = self._preprocess_image(img)
                        seals = self._detect_seals(pre)
                        if self.ocr_engine == "paddleocr":
                            text, _tables, blk = self._paddleocr_run(
                                pre, page_num, seal_boxes=seals
                            )
                            merge_block_stats(ocr_stats, blk)
                        elif self.ocr_engine == "pytesseract":
                            text = self._tesseract_run(pre)
                        else:
                            text = ""
                        if text and text.strip():
                            for p in pages:
                                if p.page_num == page_num:
                                    p.text = text
                                    p.is_ocr = True
                                    p.text_layer_chars = len(text)
                            rescued.append(page_num)
                        else:
                            unrescued.append(page_num)
                except Exception as e:
                    logger.error(f"可疑页仲裁 OCR 失败: {e}")
                unrescued = [n for n in suspect if n not in rescued]
            else:
                logger.warning(
                    f"⚠️ OCR 引擎不可用，第 {suspect} 页保持文本层结果，需人工复核"
                )
                unrescued = list(suspect)

        full_text = "\n\n".join(p.text for p in pages)
        return PdfExtractionResult(
            text=full_text,
            pages=pages,
            pdf_type="text",
            metadata={
                "pipeline": pipeline_name,
                "rescued_pages": rescued,
                "missing_pages": unrescued,
            },
        )

    # ═══════════════════════════════════════════════════════════════
    # 管线 B：扫描件 OCR
    # ═══════════════════════════════════════════════════════════════

    def _ocr_pipeline(self, pdf_bytes: bytes,
                      ocr_stats: Optional[dict] = None) -> PdfExtractionResult:
        """
        扫描件处理管线：
        PDF → 渲染为图片(PyMuPDF) → OpenCV 预处理（纠偏+去噪）→ OCR → 后处理

        Raises:
            OCREngineError: 引擎初始化失败/渲染失败——扫描件无 OCR 即无内容，
                旧版返回 "[OCR 引擎不可用]" 占位文本继续走分类是垃圾进垃圾出
        """
        if ocr_stats is None:
            ocr_stats = {}
        pages: list[PageInfo] = []
        tables: list[TableInfo] = []

        # 1) 初始化 OCR 引擎
        if not self._init_ocr():
            raise OCREngineError("OCR 引擎初始化失败，扫描件无法识别（检查模型缓存/依赖）")

        # 2) PDF → 图片（每页渲染为 PNG 字节）
        images = self._pdf_to_images(pdf_bytes)
        if not images:
            raise OCREngineError("PDF 渲染为图片失败，扫描件无法识别")

        # 3) 逐页 OCR
        for page_num, img in enumerate(images, start=1):
            logger.debug(f"  OCR 第 {page_num} 页...")

            # 3a) OpenCV 预处理（纠偏+去噪）+ 印章区块检测
            preprocessed = self._preprocess_image(img)
            seals = self._detect_seals(preprocessed)

            # 3b) 执行 OCR
            page_text = ""
            if self.ocr_engine == "paddleocr":
                page_text, page_tables, blk = self._paddleocr_run(
                    preprocessed, page_num, seal_boxes=seals
                )
                tables.extend(page_tables)
                merge_block_stats(ocr_stats, blk)
            elif self.ocr_engine == "pytesseract":
                page_text = self._tesseract_run(preprocessed)
            else:
                raise OCREngineError(f"未配置可用的 OCR 引擎: {self.ocr_engine}")

            pages.append(PageInfo(
                page_num=page_num,
                text=page_text,
                is_ocr=True,
                image_count=1,
                text_layer_chars=len(page_text),
            ))

        full_text = "\n\n".join(p.text for p in pages)
        return PdfExtractionResult(
            text=full_text,
            pages=pages,
            tables=tables,
            pdf_type="scanned",
            ocr_engine=self.ocr_engine,
            metadata={
                "image_source": "PyMuPDF",
                "preprocessing": "OpenCV(deskew+denoise)",
            },
        )

    # ═══════════════════════════════════════════════════════════════
    # 管线 C：混合 PDF（页级分流）
    # ═══════════════════════════════════════════════════════════════

    def _hybrid_pipeline(self, pdf_bytes: bytes, page_classes: list,
                         probes=None,
                         ocr_stats: Optional[dict] = None) -> PdfExtractionResult:
        """
        混合管线：按页级分类结果分流
          文字页（含 text_layered）→ fitz 快抽（可疑页仲裁兜底）
          扫描页 → 渲染 200dpi → OpenCV（纠偏+去噪）→ OCR → 后处理
          按页序拼回——两类页各走各的管线，互不污染

        扫描页 OCR 失败/文字页仲裁失败 → metadata.missing_pages
        （extract_text 写入 quality.missing_pages 强制转人工），不再静默丢内容。
        """
        if ocr_stats is None:
            ocr_stats = {}
        pages: list[PageInfo] = []
        tables: list[TableInfo] = []
        scanned_nums = [i + 1 for i, c in enumerate(page_classes) if c == "scanned"]
        text_nums = [i + 1 for i, c in enumerate(page_classes) if c != "scanned"]
        missing: list = []

        # 1) 文字页：fitz 逐页抽取
        text_page_data = {}  # page_num -> (text, image_count)
        fitz_failed = False
        try:
            import fitz
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                for i, page in enumerate(doc, start=1):
                    if i <= len(page_classes) and page_classes[i - 1] != "scanned":
                        text_page_data[i] = (
                            page.get_text() or "",
                            len(page.get_images() or []),
                        )
        except ImportError:
            logger.warning("PyMuPDF 未安装，混合管线的文字页无法抽取")
            fitz_failed = True
        except Exception as e:
            logger.warning(f"fitz 文字页抽取失败: {e}")
            fitz_failed = True
        if fitz_failed:
            missing.extend(text_nums)

        # 1.5) 文字页可疑仲裁（乱码/低字符/text_layered 失真 → OCR 替换）
        suspect: list = []
        if probes and not fitz_failed:
            suspect = [p.page_num for p, c in zip(probes, page_classes)
                       if c != "scanned" and self._page_is_suspect(p, c)]
        rescued: list = []
        if suspect and self._init_ocr():
            try:
                images = self._pdf_to_images(pdf_bytes, page_nums=set(suspect))
                for page_num, img in zip(suspect, images):
                    pre = self._preprocess_image(img)
                    seals = self._detect_seals(pre)
                    if self.ocr_engine == "paddleocr":
                        text, page_tables, blk = self._paddleocr_run(
                            pre, page_num, seal_boxes=seals
                        )
                        tables.extend(page_tables)
                        merge_block_stats(ocr_stats, blk)
                    elif self.ocr_engine == "pytesseract":
                        text = self._tesseract_run(pre)
                    else:
                        text = ""
                    if text and text.strip():
                        text_page_data[page_num] = (text, 1)
                        rescued.append(page_num)
            except Exception as e:
                logger.error(f"文字页可疑仲裁 OCR 失败: {e}")
        missing.extend(n for n in suspect if n not in rescued)

        # 2) 扫描页：渲染 + OCR（只渲染扫描页，文字页不浪费 OCR 成本）
        ocr_texts = {}
        if scanned_nums:
            if self._init_ocr():
                try:
                    images = self._pdf_to_images(
                        pdf_bytes, page_nums=set(scanned_nums)
                    )
                    for page_num, img in zip(scanned_nums, images):
                        pre = self._preprocess_image(img)
                        seals = self._detect_seals(pre)
                        if self.ocr_engine == "paddleocr":
                            text, page_tables, blk = self._paddleocr_run(
                                pre, page_num, seal_boxes=seals
                            )
                            tables.extend(page_tables)
                            merge_block_stats(ocr_stats, blk)
                        elif self.ocr_engine == "pytesseract":
                            text = self._tesseract_run(pre)
                        else:
                            text = ""
                        ocr_texts[page_num] = text
                except Exception as e:
                    logger.error(
                        f"扫描页 OCR 失败（第 {scanned_nums} 页）: {e}"
                    )
            else:
                logger.warning(
                    f"⚠️ OCR 引擎不可用，第 {scanned_nums} 页（扫描页）无法识别"
                )
        # 扫描页缺失：无有效 OCR 文本的页（识别失败或空白，保守标记人工核对）
        missing.extend(n for n in scanned_nums if not ocr_texts.get(n, "").strip())

        # 3) 按页序拼装
        for i in range(1, len(page_classes) + 1):
            if page_classes[i - 1] == "scanned":
                text = ocr_texts.get(i, "")
                pages.append(PageInfo(
                    page_num=i, text=text, is_ocr=True,
                    image_count=1, text_layer_chars=len(text),
                ))
            else:
                text, img_count = text_page_data.get(i, ("", 0))
                pages.append(PageInfo(
                    page_num=i, text=text, is_ocr=False,
                    image_count=img_count, text_layer_chars=len(text),
                ))

        full_text = "\n\n".join(p.text for p in pages)
        return PdfExtractionResult(
            text=full_text,
            pages=pages,
            tables=tables,
            pdf_type="mixed",
            ocr_engine=self.ocr_engine,
            metadata={
                "route": "hybrid",
                "scanned_pages": scanned_nums,
                "ocr_filled_pages": sorted(ocr_texts.keys()),
                "rescued_pages": rescued,
                "missing_pages": sorted(set(missing)),
            },
        )

    # ═══════════════════════════════════════════════════════════════
    # OCR 引擎初始化（延迟加载）
    # ═══════════════════════════════════════════════════════════════

    def _build_paddleocr_3x(self):
        """
        构建 PaddleOCR 3.x 实例（enable_mkldnn=False 绕开 oneDNN/PIR 兼容性 bug）

        paddleocr 3.x 经 parse_common_args 接受 enable_mkldnn 公共参数：
        CPU 下 False → engine_config.run_mode="paddle"（禁用 oneDNN）。
        paddle 3.x 的 PIR 执行器 + oneDNN 在部分 CPU 上报
        "ConvertPirAttribute2RuntimeAttribute not support" 推理崩溃。

        模型选型（2026-09 实测基准，扫描版合同 200dpi 单页 / 4核CPU）：
          - PP-OCRv6_medium（默认）：44~49s/页
          - PP-OCRv5_mobile     ：15~17s/页，行数与准确率持平略优
          → CPU 用 mobile；GPU 保持默认 medium 精度模型

        检测输入限缩（2026-09 bench_ocr_configs.py 基准）：
          - text_det_limit_side_len=736 (max)：检测图长边缩到 736px，
            单页 15~17s → 约 10.8s，行数/准确率与原图基本持平
          - cpu_threads=4：4 线程最优（8 线程无增益且耗时抖动大）

        三个前处理子模型全部关闭（实测对耗时无影响的 UVDoc/doc_ori 关闭后
        不再加载，省内存；textline_orientation 每页多花 ~4s 且合同均为正向）：
          - use_doc_orientation_classify：整图方向分类（合同扫描件固定正向）
          - use_doc_unwarping：UVDoc 弯曲矫正（针对书页弯曲，合同不适用）
          - use_textline_orientation：行级方向分类（正向文档不需要）
        """
        from paddleocr import PaddleOCR

        logger.info("🔄 首次加载 PaddleOCR（3.x, %s 模型, enable_mkldnn=False）...",
                    "medium" if self.use_gpu else "mobile")
        kwargs = dict(
            lang=self.lang,
            device="gpu" if self.use_gpu else "cpu",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
            # ── 基准测试优选参数（scripts/bench_ocr_configs.py）──
            cpu_threads=4,
            text_det_limit_side_len=736,
            text_det_limit_type="max",
        )
        if not self.use_gpu:
            kwargs["text_detection_model_name"] = "PP-OCRv5_mobile_det"
            kwargs["text_recognition_model_name"] = "PP-OCRv5_mobile_rec"
        return PaddleOCR(**kwargs)

    def _init_ocr(self) -> bool:
        """延迟加载 OCR 引擎，返回是否成功（双检锁防并发双重初始化）"""
        if self._ocr_initialized:
            return True
        with self._ocr_lock:
            if self._ocr_initialized:
                return True
            return self._init_ocr_locked()

    def _init_ocr_locked(self) -> bool:
        """实际初始化（必须持 _ocr_lock 调用）"""
        try:
            if self.ocr_engine == "paddleocr":
                from paddleocr import PaddleOCR
                # PaddleOCR 3.x 移除了 use_gpu/show_log（改 device 参数）、
                # use_angle_cls 改名 use_textline_orientation；这里先试 3.x
                # 参数，失败回退 2.x，兼容两个大版本。
                # 注意：paddle 3.x 的 PIR 执行器与 oneDNN 在部分 CPU 上推理
                # 崩溃（ConvertPirAttribute2RuntimeAttribute not support），
                # 3.x 通过 enable_mkldnn=False 显式禁用 oneDNN。
                try:
                    self._ocr = self._build_paddleocr_3x()
                except (ImportError, TypeError, ValueError) as e:
                    logger.warning(f"PaddleOCR 3.x 初始化降级（{e}），尝试兼容模式")
                    try:
                        self._ocr = PaddleOCR(
                            lang=self.lang,
                            device="gpu" if self.use_gpu else "cpu",
                            use_textline_orientation=True,
                        )
                    except (TypeError, ValueError):
                        self._ocr = PaddleOCR(
                            use_angle_cls=True,
                            lang=self.lang,
                            use_gpu=self.use_gpu,
                            show_log=False,
                        )
                # 可选：PP-Structure（表格识别）
                # 2.x 类名 PPStructure；3.x 改名 PPStructureV3（首次使用会自动下载模型）
                if self.enable_table_detect:
                    try:
                        from paddleocr import PPStructure  # 2.x
                        self._table_engine = PPStructure(
                            use_gpu=self.use_gpu, show_log=False
                        )
                        logger.info("✅ PP-Structure 表格引擎已加载")
                    except ImportError:
                        try:
                            from paddleocr import PPStructureV3  # 3.x
                            self._table_engine = PPStructureV3(
                                device="gpu" if self.use_gpu else "cpu",
                                use_doc_orientation_classify=False,
                                use_doc_unwarping=False,
                                use_textline_orientation=False,
                            )
                            logger.info("✅ PP-StructureV3 表格引擎已加载（3.x）")
                        except Exception as e:
                            logger.warning(f"⚠️ PP-Structure 加载失败: {e}")
                            self._table_engine = None
                    except Exception as e:
                        logger.warning(f"⚠️ PP-Structure 加载失败: {e}")
                        self._table_engine = None
                logger.info("✅ PaddleOCR 加载完成")

            elif self.ocr_engine == "pytesseract":
                import pytesseract
                self._ocr = pytesseract
                logger.info("✅ PyTesseract 就绪")

            else:
                logger.error(f"未知 OCR 引擎: {self.ocr_engine}")
                return False

            self._ocr_initialized = True
            return True

        except ImportError as e:
            logger.error(f"OCR 引擎导入失败: {e}")
            return False
        except Exception as e:
            logger.error(f"OCR 引擎初始化失败: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 内部：PDF → 图片
    # ═══════════════════════════════════════════════════════════════

    def _pdf_to_images(self, pdf_bytes: bytes, dpi: int = 200,
                       page_nums=None) -> list:
        """
        用 PyMuPDF (fitz) 把 PDF 每页渲染为 numpy.ndarray（OpenCV 兼容格式）

        比 pdf2image + poppler 快 3-5x，且不需要系统安装 poppler。
        默认 200dpi：OCR 识别的标准分辨率，相比 300dpi 像素量降 2.25 倍，
        显著降低 CPU 推理耗时与内存峰值（低配 Docker VM 防 OOM）。

        page_nums: 仅渲染指定页（1-based 集合），None=全部页。
                   页级分流/救援用——只渲染扫描页，避免整份渲染浪费。
        """
        images = []
        try:
            import fitz  # PyMuPDF
            import numpy as np

            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)

            for page_idx, page in enumerate(doc, start=1):
                if page_nums is not None and page_idx not in page_nums:
                    continue
                # 渲染为 pixmap → numpy array
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )
                # RGB → BGR（OpenCV 默认）——纯 numpy 切片，避免渲染依赖 cv2
                # （此前 import cv2 在 try 块内，cv2 缺失会被误报为"PyMuPDF 未安装"）
                if pix.n == 3:
                    img = np.ascontiguousarray(img[:, :, ::-1])
                images.append(img)

            doc.close()
            logger.debug(f"  PDF 渲染: {len(images)} 页, DPI={dpi}")

        except ImportError:
            logger.warning("PyMuPDF 未安装，尝试 pdf2image 兜底")
            images = self._pdf_to_images_pdf2image(pdf_bytes, dpi)
        except Exception as e:
            logger.error(f"PDF 转图片失败: {e}")

        return images

    def _pdf_to_images_pdf2image(self, pdf_bytes: bytes, dpi: int = 300) -> list:
        """pdf2image 兜底方案（需要系统装 poppler-utils）"""
        try:
            from pdf2image import convert_from_bytes
            import numpy as np
            import cv2

            pil_images = convert_from_bytes(pdf_bytes, dpi=dpi)
            result = []
            for pil in pil_images:
                arr = np.array(pil)
                arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                result.append(arr)
            return result
        except ImportError:
            logger.error("pdf2image 也不可用")
            return []
        except Exception as e:
            logger.error(f"pdf2image 转换失败: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════
    # 内部：OpenCV 图像预处理
    # ═══════════════════════════════════════════════════════════════

    def _preprocess_image(self, img):
        """
        OpenCV 图像预处理管线：灰度 → 倾斜矫正（>1°）→ 高斯去噪

        ⚠️ 不做自适应二值化（2026-09 实测）：PaddleOCR 检测/识别模型在自然
        灰度图上训练，硬二值化丢失笔画反锯齿细节反而降准确率——同一扫描页
        实测：二值图 822 字符 30 行（"签订时间"误识为"签订时问"），
        灰度去噪图 826 字符 32 行零错字。

        Args:
            img: numpy.ndarray (BGR 或灰度)

        Returns:
            预处理后的 numpy.ndarray
        """
        try:
            import cv2
            import numpy as np

            # 1) 灰度化
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img.copy()

            # 2) 倾斜矫正（歪斜扫描件：HoughLinesP 检测文本行角度，
            #    中位数稳健；|角度| < 1° 自动跳过，表格横线主导时≈0°不受影响）
            gray = self._deskew(gray)

            # 3) 高斯滤波去噪（核 5x5）
            denoised = cv2.GaussianBlur(gray, (5, 5), 0)

            # PaddleOCR 3.x predict() 要求 3 通道输入，2D 灰度会报
            # "tuple index out of range"，这里统一转回 3 通道 BGR
            logger.debug("  图像预处理: 灰度→纠偏→去噪→BGR")
            return cv2.cvtColor(denoised, cv2.COLOR_GRAY2BGR)

        except ImportError:
            logger.warning("OpenCV 不可用，返回原图")
            return img
        except Exception as e:
            logger.warning(f"图像预处理失败，返回原图: {e}")
            return img

    @staticmethod
    def _deskew(img):
        """
        倾斜矫正（可选步骤）

        原理：检测文本区域 → 计算角度 → warpAffine 旋转回去
        只有倾斜角 > 1° 时才触发（避免过度矫正）
        """
        try:
            import cv2
            import numpy as np

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
            edges = cv2.Canny(gray, 50, 150)
            lines = cv2.HoughLinesP(edges, 1, np.pi / 180,
                                    threshold=100, minLineLength=100, maxLineGap=10)

            if lines is None or len(lines) == 0:
                return img

            # 计算平均角度
            angles = []
            for line in lines:
                x1, y1, x2, y2 = line[0]
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                if -45 < angle < 45:
                    angles.append(angle)

            if not angles:
                return img

            median_angle = np.median(angles)
            if abs(median_angle) < 1.0:
                return img  # 倾斜太小，跳过

            h, w = img.shape[:2]
            M = cv2.getRotationMatrix2D((w / 2, h / 2), median_angle, 1.0)
            rotated = cv2.warpAffine(
                img, M, (w, h),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
            logger.debug(f"  倾斜矫正: {median_angle:.1f}°")
            return rotated

        except Exception as e:
            logger.debug(f"倾斜矫正跳过: {e}")
            return img

    # ═══════════════════════════════════════════════════════════════
    # 内部：OCR 引擎执行
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _result_field(res, key: str):
        """
        从 OCR 结果对象中安全取字段。

        兼容三种形态：
          - dict（2.x / 部分版本）
          - OCRResult（3.x，实现 __getitem__ 但非 dict 子类）
          - 普通对象属性
        """
        try:
            v = res[key]
            return v
        except Exception:
            return getattr(res, key, None)

    def _paddleocr_run(self, img, page_num: int,
                       seal_boxes: Optional[list] = None) -> tuple[str, list[TableInfo], dict]:
        """
        PaddleOCR 识别单页（推理持锁——PaddleOCR/PP-Structure 实例均非线程安全）

        后处理链（2026-09 版面分析升级）：
          ① 阅读顺序：rec_boxes 坐标 → 行聚类（y 中线）→ 行内按 x 排序
             （双栏/表格版面不再依赖模型输出顺序）
          ② 段落合并：行间距 > 行高中位 ×1.8 → 段落空行
          ③ 印章区块：文本块中心落入红章框 → seal_overlap 人工靶点
          ④ 词汇纠错：合同错字词典，仅低置信块应用，逐处记录可回溯
          ⑤ 置信度分级：conf<0.85 低置信靶点 / conf≤0.5 丢弃

        Returns:
            (文本, 表格列表, 块统计)
            块统计: {"total", "low_conf", "corrections", "seal_overlapped"}
        """
        if self._ocr is None:
            return "", [], {"total": 0, "low_conf": []}

        tables: list[TableInfo] = []
        block_stats: dict = {"total": 0, "low_conf": [], "corrections": [],
                             "seal_overlapped": 0}

        # ── 推理（持锁）──
        raw, use_v3 = None, True
        try:
            with self._ocr_lock:
                if hasattr(self._ocr, "predict"):
                    raw = self._ocr.predict(img)          # 3.x: list[OCRResult]
                else:
                    raw = self._ocr.ocr(img, cls=True)    # 2.x: 嵌套 list
                    use_v3 = False
        except Exception as e:
            logger.warning(f"PaddleOCR 识别异常 (p{page_num}): {e}")

        # ── 解析为 (text, conf, box) 块列表（数据已取回，无需持锁）──
        blocks: list[tuple] = []
        if use_v3:
            for res in raw or []:
                texts = self._result_field(res, "rec_texts")
                scores = self._result_field(res, "rec_scores")
                boxes = self._result_field(res, "rec_boxes")
                if not texts:
                    continue
                for j in range(len(texts)):
                    conf = 1.0
                    if scores and j < len(scores) and scores[j] is not None:
                        try:
                            conf = float(scores[j])
                        except (TypeError, ValueError):
                            pass
                    box = None
                    if boxes is not None:
                        try:
                            box = [int(boxes[j][0]), int(boxes[j][1]),
                                   int(boxes[j][2]), int(boxes[j][3])]
                        except Exception:
                            box = None
                    blocks.append((texts[j], conf, box))
        elif raw and len(raw) > 0 and raw[0]:
            for line in raw[0]:
                try:
                    text = line[1][0] if isinstance(line[1], (list, tuple)) else str(line[1])
                    conf = 0.0
                    if isinstance(line[1], (list, tuple)) and line[1][1] is not None:
                        conf = float(line[1][1])
                    box = None
                    if isinstance(line[0], (list, tuple)) and len(line[0]) >= 3:
                        box = [int(line[0][0][0]), int(line[0][0][1]),
                               int(line[0][2][0]), int(line[0][2][1])]
                    blocks.append((text, conf, box))
                except (IndexError, TypeError):
                    continue

        # ── 块级统计 + 印章打标 + 词典纠错 ──
        kept: list[tuple] = []
        for text, conf, box in blocks:
            block_stats["total"] += 1
            if not text:
                continue
            if conf <= OCR_DROP_CONF_SCORE:
                # 低置信被丢弃 → 该内容在文本中缺失，风险最高，必须记录
                block_stats["low_conf"].append({
                    "page": page_num, "text": text[:50],
                    "score": round(conf, 3), "dropped": True,
                })
                continue
            corrected = text
            if conf < OCR_LOW_CONF_SCORE:
                block_stats["low_conf"].append({
                    "page": page_num, "text": text[:50],
                    "score": round(conf, 3), "dropped": False,
                })
                corrected = self._apply_glossary(text, page_num, conf, block_stats)
            if seal_boxes and box and self._box_in_seal(box, seal_boxes):
                # 印章压字区域识别结果天然不可信——即使高置信也强制人工靶点
                block_stats["seal_overlapped"] += 1
                block_stats["low_conf"].append({
                    "page": page_num, "text": text[:50],
                    "score": round(conf, 3), "dropped": False,
                    "reason": "seal_overlap",
                })
            kept.append((corrected, conf, box))

        # ── PP-Structure 表格识别（持锁，与 OCR 同为 paddle 预测器）──
        if self._table_engine is not None:
            try:
                with self._ocr_lock:
                    table_result = self._table_engine(img)
                for item in table_result or []:
                    if item.get("type") == "table":
                        res = item.get("res", {})
                        html = res.get("html", "")
                        # 表格还原：HTML → 二维数组（下游 /extract 响应 tables 字段）
                        rows = self._html_table_to_rows(html)
                        tables.append(TableInfo(
                            page_num=page_num, rows=rows, html=html,
                        ))
            except Exception as e:
                logger.warning(f"PP-Structure 表格识别异常 (p{page_num}): {e}")

        # ── 文本拼接：阅读顺序 + 段落合并 ──
        page_text = self._reading_order_text(kept)
        return page_text, tables, block_stats

    # ═══════════════════════════════════════════════════════════════
    # 内部：版面分析后处理（阅读顺序/段落/印章/纠错）
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _reading_order_text(items: list[tuple]) -> str:
        """
        阅读顺序还原 + 段落合并。

        行聚类阈值 = 行高中位 ×0.6（同一行的两块 y 中线差很小）；
        段落空行 = 相邻行顶距 > 行高中位 ×1.8。
        坐标缺失块占比高时退回模型输出顺序（PaddleOCR 默认大致从上到下）。
        """
        fallback = "\n".join(t for t, _, _ in items)
        with_box = [it for it in items if it[2] is not None]
        if len(with_box) < 2 or len(with_box) < len(items) * 0.6:
            return fallback
        heights = [b[3] - b[1] for _, _, b in with_box if b[3] > b[1]]
        if not heights:
            return fallback
        med_h = sorted(heights)[len(heights) // 2]
        row_thr = max(med_h * 0.6, 1.0)

        items_sorted = sorted(with_box, key=lambda it: (it[2][1] + it[2][3]) / 2.0)
        rows, cur, cur_yc = [], [], None
        for it in items_sorted:
            yc = (it[2][1] + it[2][3]) / 2.0
            if cur_yc is None or abs(yc - cur_yc) <= row_thr:
                cur.append(it)
                cur_yc = yc if cur_yc is None else (cur_yc + yc) / 2.0
            else:
                rows.append(cur)
                cur, cur_yc = [it], yc
        if cur:
            rows.append(cur)

        lines: list[str] = []
        prev_y1 = None
        for row in rows:
            row = sorted(row, key=lambda it: it[2][0])
            y1 = min(it[2][1] for it in row)
            if prev_y1 is not None and (y1 - prev_y1) > med_h * 1.8:
                lines.append("")  # 段落空行
            lines.append(" ".join(t for t, _, _ in row))
            prev_y1 = y1
        return "\n".join(lines)

    @staticmethod
    def _apply_glossary(text: str, page_num: int, conf: float,
                        block_stats: dict) -> str:
        """合同领域词典纠错（仅低置信块调用）；每处替换记录进 corrections 可回溯"""
        corrected = text
        for wrong, right in CONTRACT_TYPO_DICT.items():
            if wrong in corrected:
                block_stats["corrections"].append({
                    "page": page_num, "from": wrong, "to": right,
                    "context": text[:50], "score": round(conf, 3),
                })
                corrected = corrected.replace(wrong, right)
        return corrected

    @staticmethod
    def _detect_seals(img) -> list:
        """
        红色印章区块检测（HSV 双区间 + 形态学闭运算 + 连通域）。

        Returns:
            [(x1, y1, x2, y2)]；cv2 不可用/无印章返回 []
        """
        try:
            import cv2
            import numpy as np
            if img is None or len(img.shape) < 2:
                return []
            h, w = img.shape[:2]
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, np.array(SEAL_HSV_LOWER1), np.array(SEAL_HSV_UPPER1))
            mask |= cv2.inRange(hsv, np.array(SEAL_HSV_LOWER2), np.array(SEAL_HSV_UPPER2))
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            seals = []
            min_area = w * h * SEAL_MIN_AREA_RATIO
            for c in contours or []:
                if cv2.contourArea(c) < min_area:
                    continue
                x, y, bw, bh = cv2.boundingRect(c)
                seals.append((x, y, x + bw, y + bh))
            return seals
        except Exception:
            return []

    @staticmethod
    def _box_in_seal(box, seal_boxes) -> bool:
        """文本块中心落入印章框 → 视为印章压字块"""
        cx = (box[0] + box[2]) / 2.0
        cy = (box[1] + box[3]) / 2.0
        for sx1, sy1, sx2, sy2 in seal_boxes:
            if sx1 <= cx <= sx2 and sy1 <= cy <= sy2:
                return True
        return False

    def _tesseract_run(self, img) -> str:
        """PyTesseract 识别单页（备选）"""
        if self._ocr is None:
            return ""
        try:
            return self._ocr.image_to_string(img, lang=self.lang)
        except Exception as e:
            logger.warning(f"PyTesseract 识别异常: {e}")
            return ""

    @staticmethod
    def _html_table_to_rows(html: str) -> list[list[str]]:
        """PP-Structure 返回的 HTML 表格 → 二维数组"""
        rows: list[list[str]] = []
        try:
            from html.parser import HTMLParser

            class _TableParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.current_row = []
                    self.rows = []
                    self.in_cell = False
                    self.cell_text = ""

                def handle_starttag(self, tag, attrs):
                    if tag == "tr":
                        self.current_row = []
                    elif tag in ("td", "th"):
                        self.in_cell = True
                        self.cell_text = ""

                def handle_endtag(self, tag):
                    if tag == "tr":
                        if self.current_row:
                            self.rows.append(self.current_row)
                    elif tag in ("td", "th"):
                        self.in_cell = False
                        self.current_row.append(self.cell_text.strip())

                def handle_data(self, data):
                    if self.in_cell:
                        self.cell_text += data

            parser = _TableParser()
            parser.feed(html)
            rows = parser.rows
        except Exception:
            pass
        return rows


# ─────────────────────────────────────────────────────────────────────
# 便捷函数
# ─────────────────────────────────────────────────────────────────────
def process_pdf(
    pdf_bytes: bytes,
    ocr_engine: str = "paddleocr",
    **kwargs,
) -> PdfExtractionResult:
    """一行调用"""
    proc = PdfProcessor(ocr_engine=ocr_engine, **kwargs)
    return proc.extract_text(pdf_bytes)

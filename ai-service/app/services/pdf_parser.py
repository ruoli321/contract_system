# ═══════════════════════════════════════════════════════════════════
# M6 · PDF 预处理与 OCR 分流
#
# 类 PdfProcessor：自动检测 PDF 类型 → 分流处理
#   - 文字版（有文本层） → pdfplumber 直接提取
#   - 扫描版（无文本层） → OpenCV 图像预处理 → PaddleOCR → PP-Structure 表格
#
# 返回值 PdfExtractionResult：结构化数据
#   text        — 合并后的纯文本
#   pages       — 每页信息（页码、文本、图片数、是否 OCR）
#   tables      — 识别到的表格（PP-Structure 结果）
#   pdf_type    — "text" / "scanned"
#   ocr_engine  — 使用的 OCR 引擎
#   metadata    — 页数、处理耗时、引擎版本等
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import io
import time
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

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容 dict"""
        return asdict(self)


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
    SCANNED_TEXT_THRESHOLD = 50       # 清洗后字符数 < 此值视为扫描件
    MIXED_MIN_CHARS = 100             # > 此值判定为 mixed（文字+扫描混合）

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

    # ═══════════════════════════════════════════════════════════════
    # 公开 API
    # ═══════════════════════════════════════════════════════════════

    def detect_type(self, pdf_bytes: bytes) -> str:
        """
        检测 PDF 类型

        Returns:
            "text"    — 纯文字版（有文本层）
            "scanned" — 纯扫描件（无文本层）
            "mixed"   — 混合（有文本层但文本很少，可能夹带扫描页）
            "unknown" — 检测失败
        """
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
        主入口：自动判定 → 分流处理 → 返回结构化结果

        Args:
            pdf_bytes: PDF 文件的二进制内容

        Returns:
            PdfExtractionResult
        """
        t0 = time.time()

        # 1) 检测类型
        pdf_type = self.detect_type(pdf_bytes)
        logger.info(f"📄 PDF 类型检测: {pdf_type}")

        # 2) 分流
        if pdf_type == "text":
            result = self._text_pipeline(pdf_bytes)
        elif pdf_type in ("scanned", "mixed"):
            result = self._ocr_pipeline(pdf_bytes)
            result.pdf_type = pdf_type
        else:
            result = PdfExtractionResult(
                text="", pdf_type="unknown", metadata={"error": "检测失败"}
            )

        # 3) 记录耗时
        result.metadata["elapsed_seconds"] = round(time.time() - t0, 3)
        result.metadata["page_count"] = len(result.pages)
        result.metadata["ocr_engine"] = self.ocr_engine

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
        cleaned = "".join(str(total_chars))
        has_text = total_chars > self.SCANNED_TEXT_THRESHOLD
        logger.debug(
            f"  文本层检测: {page_count} 页, {total_chars} 字符 → "
            f"{'有文本层' if has_text else '无文本层'}"
        )
        return has_text, total_chars

    # ═══════════════════════════════════════════════════════════════
    # 管线 A：文字版 PDF
    # ═══════════════════════════════════════════════════════════════

    def _text_pipeline(self, pdf_bytes: bytes) -> PdfExtractionResult:
        """pdfplumber 直接抽取文本"""
        pages: list[PageInfo] = []
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
        except ImportError:
            logger.error("pdfplumber 未安装")
        except Exception as e:
            logger.error(f"pdfplumber 抽取失败: {e}")
            # 兜底：pypdf
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(pdf_bytes))
                for i, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    pages.append(PageInfo(
                        page_num=i + 1, text=text, is_ocr=False,
                    ))
            except Exception as e2:
                logger.error(f"pypdf 兜底也失败: {e2}")

        full_text = "\n\n".join(p.text for p in pages)
        return PdfExtractionResult(
            text=full_text,
            pages=pages,
            pdf_type="text",
            metadata={"pipeline": "pdfplumber"},
        )

    # ═══════════════════════════════════════════════════════════════
    # 管线 B：扫描件 OCR
    # ═══════════════════════════════════════════════════════════════

    def _ocr_pipeline(self, pdf_bytes: bytes) -> PdfExtractionResult:
        """
        扫描件处理管线：
        PDF → 渲染为图片(PyMuPDF) → OpenCV 预处理 → OCR → PP-Structure 表格
        """
        pages: list[PageInfo] = []
        tables: list[TableInfo] = []

        # 1) 初始化 OCR 引擎
        if not self._init_ocr():
            return PdfExtractionResult(
                text="[OCR 引擎不可用]", pdf_type="scanned",
                ocr_engine=self.ocr_engine,
                metadata={"error": "OCR engine init failed"},
            )

        # 2) PDF → 图片（每页渲染为 PNG 字节）
        images = self._pdf_to_images(pdf_bytes)
        if not images:
            return PdfExtractionResult(
                text="[PDF 转图片失败]", pdf_type="scanned",
                ocr_engine=self.ocr_engine,
            )

        # 3) 逐页 OCR
        for page_num, img in enumerate(images, start=1):
            logger.debug(f"  OCR 第 {page_num} 页...")

            # 3a) OpenCV 预处理
            preprocessed = self._preprocess_image(img)

            # 3b) 执行 OCR
            page_text = ""
            if self.ocr_engine == "paddleocr":
                page_text, page_tables = self._paddleocr_run(
                    preprocessed, page_num
                )
                tables.extend(page_tables)
            elif self.ocr_engine == "pytesseract":
                page_text = self._tesseract_run(preprocessed)
            else:
                page_text = "[未配置 OCR 引擎]"

            pages.append(PageInfo(
                page_num=page_num,
                text=page_text,
                is_ocr=True,
                image_count=1,
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
                "preprocessing": "OpenCV",
            },
        )

    # ═══════════════════════════════════════════════════════════════
    # OCR 引擎初始化（延迟加载）
    # ═══════════════════════════════════════════════════════════════

    def _init_ocr(self) -> bool:
        """延迟加载 OCR 引擎，返回是否成功"""
        if self._ocr_initialized:
            return True

        try:
            if self.ocr_engine == "paddleocr":
                logger.info("🔄 首次加载 PaddleOCR...")
                from paddleocr import PaddleOCR
                self._ocr = PaddleOCR(
                    use_angle_cls=True,
                    lang=self.lang,
                    use_gpu=self.use_gpu,
                    show_log=False,
                )
                # 可选：PP-Structure（表格识别）
                if self.enable_table_detect:
                    try:
                        from paddleocr import PPStructure
                        self._table_engine = PPStructure(
                            use_gpu=self.use_gpu, show_log=False
                        )
                        logger.info("✅ PP-Structure 表格引擎已加载")
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

    def _pdf_to_images(self, pdf_bytes: bytes, dpi: int = 300) -> list:
        """
        用 PyMuPDF (fitz) 把 PDF 每页渲染为 numpy.ndarray（OpenCV 兼容格式）

        比 pdf2image + poppler 快 3-5x，且不需要系统安装 poppler
        """
        images = []
        try:
            import fitz  # PyMuPDF
            import numpy as np

            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)

            for page in doc:
                # 渲染为 pixmap → numpy array
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )
                # RGB → BGR（OpenCV 默认）
                if pix.n == 3:
                    import cv2
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
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
        OpenCV 图像预处理管线：
        灰度 → 高斯去噪 → 二值化（自适应）→ 可选纠偏

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

            # 2) 高斯滤波去噪（核 5x5）
            denoised = cv2.GaussianBlur(gray, (5, 5), 0)

            # 3) 自适应二值化（比全局 Otsu 对光照不均更稳）
            binary = cv2.adaptiveThreshold(
                denoised, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                blockSize=15, C=8,
            )

            logger.debug("  图像预处理: 灰度→去噪→自适应二值化")
            return binary

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

    def _paddleocr_run(self, img, page_num: int) -> tuple[str, list[TableInfo]]:
        """
        PaddleOCR 识别单页

        Returns:
            (文本, 表格列表)
        """
        if self._ocr is None:
            return "", []

        tables: list[TableInfo] = []
        all_text_lines = []

        try:
            # PaddleOCR 3.x: ocr(img, cls=True)
            # 返回 [[[bbox, (text, conf)], ...]]  （batch 外层）
            result = self._ocr.ocr(img, cls=True)

            # 兼容 2.x / 3.x 返回格式
            if result and len(result) > 0 and result[0]:
                for line in result[0]:
                    try:
                        # line = [bbox, (text, confidence)]
                        text = line[1][0] if isinstance(line[1], (list, tuple)) else str(line[1])
                        conf = line[1][1] if isinstance(line[1], (list, tuple)) else 0.0
                        # 过滤低置信度（< 0.5）和空文本
                        if text and conf > 0.5:
                            all_text_lines.append(text)
                    except (IndexError, TypeError):
                        continue

        except Exception as e:
            logger.warning(f"PaddleOCR 识别异常 (p{page_num}): {e}")

        # PP-Structure 表格识别（可选）
        if self._table_engine is not None:
            try:
                table_result = self._table_engine(img)
                for item in table_result:
                    if item.get("type") == "table":
                        res = item.get("res", {})
                        html = res.get("html", "")
                        # 解析 HTML 为二维数组
                        rows = self._html_table_to_rows(html)
                        tables.append(TableInfo(
                            page_num=page_num, rows=rows, html=html,
                        ))
            except Exception as e:
                logger.warning(f"PP-Structure 表格识别异常 (p{page_num}): {e}")

        page_text = "\n".join(all_text_lines)
        return page_text, tables

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

# ═══════════════════════════════════════════════════════════════════
# OCR 速度基准：定位扫描版 84 秒的瓶颈
#   配置A: 二值化预处理图 + use_textline_orientation=True   （当前线上行为）
#   配置B: 二值化预处理图 + use_textline_orientation=False
#   配置C: 原始渲染图   + use_textline_orientation=False
# 运行: docker exec contract-ai python /app/scripts/benchmark_ocr.py
# ═══════════════════════════════════════════════════════════════════
import sys
import time
from pathlib import Path

import fitz
import numpy as np

sys.path.insert(0, "/app")
from app.services.pdf_parser import PdfProcessor  # noqa: E402

PDF = "/test_pdfs/manual/扫描版_房屋租赁合同_ZL2025-007.pdf"


def render_page(pdf_path: str, page_no: int = 0, dpi: int = 200) -> np.ndarray:
    doc = fitz.open(pdf_path)
    page = doc[page_no]
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    import cv2
    if pix.n == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    doc.close()
    return img


def main() -> None:
    proc = PdfProcessor()
    if not proc._init_ocr():
        print("OCR init failed")
        return
    ocr = proc._ocr
    has_predict = hasattr(ocr, "predict")
    print(f"predict API: {has_predict}")

    raw_img = render_page(PDF)
    bin_img = proc._preprocess_image(raw_img)
    print(f"raw_img={raw_img.shape}, bin_img={bin_img.shape}")

    def run(img, label: str, use_tlo: bool):
        t0 = time.time()
        if has_predict:
            result = ocr.predict(img, use_textline_orientation=use_tlo)
            texts = []
            for res in result or []:
                t = PdfProcessor._result_field(res, "rec_texts")
                if t:
                    texts.extend(t)
        else:
            result = ocr.ocr(img, cls=use_tlo)
            texts = [line[1][0] for line in (result[0] or []) if line[1]]
        dt = time.time() - t0
        joined = " ".join(texts)
        print(f"{label}: {dt:.1f}s | lines={len(texts)} | chars={len(joined)}")
        print(f"   head: {joined[:60]}")
        return dt

    print("--- A: 预处理二值图 + textline_orientation=True (线上现状) ---")
    run(bin_img, "A", True)
    print("--- B: 预处理二值图 + textline_orientation=False ---")
    run(bin_img, "B", False)
    print("--- C: 原图 + textline_orientation=False ---")
    run(raw_img, "C", False)
    print("--- D: 原图 + textline_orientation=True ---")
    run(raw_img, "D", True)


if __name__ == "__main__":
    main()

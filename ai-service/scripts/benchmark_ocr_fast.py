# 验证：禁用 doc 方向分类 + UVDoc 弯曲矫正后的 OCR 速度与质量
# 运行: docker exec contract-ai python /app/scripts/benchmark_ocr_fast.py
import sys
import time

import fitz

sys.path.insert(0, "/app")

PDF = "/test_pdfs/manual/扫描版_房屋租赁合同_ZL2025-007.pdf"


def render_page(pdf_path: str, page_no: int = 0, dpi: int = 200):
    doc = fitz.open(pdf_path)
    page = doc[page_no]
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    import numpy as np
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    import cv2
    if pix.n == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    doc.close()
    return img


def main():
    from paddleocr import PaddleOCR

    print("构建快速配置引擎（doc_ori=False, unwarp=False, textline_ori=False, mkldnn=False）...")
    t0 = time.time()
    ocr = PaddleOCR(
        lang="ch",
        device="cpu",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
    )
    print(f"引擎构建耗时 {time.time() - t0:.1f}s")

    for label, use_pre in (("E: 快速引擎+预处理二值图", True), ("F: 快速引擎+原图", False)):
        img = render_page(PDF)
        if use_pre:
            from app.services.pdf_parser import PdfProcessor
            proc = PdfProcessor()
            img = proc._preprocess_image(img)
        t0 = time.time()
        result = ocr.predict(img)
        texts = []
        for res in result or []:
            t = res.get("rec_texts") if isinstance(res, dict) else getattr(res, "rec_texts", None)
            if t:
                texts.extend(t)
        dt = time.time() - t0
        joined = " ".join(texts)
        print(f"{label}: {dt:.1f}s | lines={len(texts)} | chars={len(joined)}")
        print(f"   head: {joined[:80]}")


if __name__ == "__main__":
    main()

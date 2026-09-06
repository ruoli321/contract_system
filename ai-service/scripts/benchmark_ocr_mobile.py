# 验证 PP-OCRv5_mobile 轻量模型的速度与质量
# 运行: docker exec contract-ai python /app/scripts/benchmark_ocr_mobile.py
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

    print("构建 mobile 引擎（PP-OCRv5_mobile det+rec）...")
    t0 = time.time()
    ocr = PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        lang="ch",
        device="cpu",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
    )
    print(f"引擎构建耗时 {time.time() - t0:.1f}s")

    for page_no in (0, 1):
        img = render_page(PDF, page_no)
        t0 = time.time()
        result = ocr.predict(img)
        texts = []
        for res in result or []:
            t = res.get("rec_texts") if isinstance(res, dict) else getattr(res, "rec_texts", None)
            if t:
                texts.extend(t)
        dt = time.time() - t0
        joined = " ".join(texts)
        print(f"page{page_no + 1}: {dt:.1f}s | lines={len(texts)} | chars={len(joined)}")
        print(f"   head: {joined[:80]}")


if __name__ == "__main__":
    main()

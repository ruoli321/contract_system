"""组合 benchmark：找最快可用的 OCR 配置（单页）"""
import time

import cv2
import numpy as np
import pymupdf
from paddleocr import PaddleOCR

PDF = "/test_pdfs/manual/扫描版_房屋租赁合同_ZL2025-007.pdf"

doc = pymupdf.open(PDF)
pix = doc[0].get_pixmap(dpi=200)
img_bytes = pix.tobytes("png")
img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
img = cv2.cvtColor(cv2.GaussianBlur(gray, (5, 5), 0), cv2.COLOR_GRAY2BGR)
print(f"page1: {img.shape[1]}x{img.shape[0]}")

CONFIGS = [
    ("v5-mobile mkldnn recbs16", dict(text_recognition_model_name="PP-OCRv5_mobile_rec",
     text_detection_model_name="PP-OCRv5_mobile_det", enable_mkldnn=True, text_recognition_batch_size=16)),
    ("v4-mobile mkldnn recbs16", dict(text_recognition_model_name="PP-OCRv4_mobile_rec",
     text_detection_model_name="PP-OCRv4_mobile_det", enable_mkldnn=True, text_recognition_batch_size=16)),
    ("v4-mobile nomkldnn recbs32", dict(text_recognition_model_name="PP-OCRv4_mobile_rec",
     text_detection_model_name="PP-OCRv4_mobile_det", enable_mkldnn=False, text_recognition_batch_size=32)),
    ("v5-server-det+mobile-rec mkldnn", dict(text_recognition_model_name="PP-OCRv5_mobile_rec",
     text_detection_model_name="PP-OCRv5_server_det", enable_mkldnn=True, text_recognition_batch_size=16)),
]

BASE = dict(lang="ch", device="cpu", use_doc_orientation_classify=False,
            use_doc_unwarping=False, use_textline_orientation=False)

for name, extra in CONFIGS:
    try:
        t0 = time.time()
        ocr = PaddleOCR(**BASE, **extra)
        t_init = time.time() - t0
        t1 = time.time()
        res = ocr.predict(img)
        dt = time.time() - t1
        texts = []
        for r in res:
            texts.extend(r["rec_texts"])
        joined = "".join(texts)
        print(f"[OK] {name}: init={t_init:.1f}s ocr={dt:.1f}s lines={len(texts)} chars={len(joined)} sample={joined[:30]}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:120]}")

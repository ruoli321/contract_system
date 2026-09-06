"""容器内 benchmark：验证 text_det_limit_side_len 参数并测单页 OCR 耗时"""
import time

from paddleocr import PaddleOCR

PDF = "/test_pdfs/manual/扫描版_房屋租赁合同_ZL2025-007.pdf"

import pymupdf

doc = pymupdf.open(PDF)
pix = doc[0].get_pixmap(dpi=200)
img_bytes = pix.tobytes("png")
print(f"page1 png: {len(img_bytes)//1024} KB")

import cv2
import numpy as np

img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
img = cv2.cvtColor(cv2.GaussianBlur(gray, (5, 5), 0), cv2.COLOR_GRAY2BGR)

for side_len in (960, 736):
    t0 = time.time()
    ocr = PaddleOCR(
        lang="ch",
        device="cpu",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
        text_det_limit_side_len=side_len,
        text_det_limit_type="max",
    )
    print(f"init side_len={side_len}: {time.time()-t0:.1f}s")
    t1 = time.time()
    res = ocr.predict(img)
    texts = []
    for r in res:
        texts.extend(r["rec_texts"])
    dt = time.time() - t1
    joined = "".join(texts)
    print(f"side_len={side_len}: OCR {dt:.1f}s, lines={len(texts)}, chars={len(joined)}, sample={joined[:40]}")

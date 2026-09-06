"""尝试让 mkldnn 可用：禁 PIR / run_mode 透传"""
import os

os.environ["FLAGS_enable_pir_api"] = "0"
os.environ["FLAGS_enable_pir_in_executor"] = "0"

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

import paddle
print("paddle:", paddle.__version__)

# 看 paddlex Option 选项
try:
    from paddlex.utils.pipeline_options import PaddlePredictorOption
    opt = PaddlePredictorOption()
    print("run_mode options:", getattr(opt, "run_mode", None))
    attrs = [a for a in dir(opt) if not a.startswith("_")]
    print("opt attrs:", attrs)
except Exception as e:
    print("opt introspect fail:", e)

CONFIGS = [
    ("v5-mobile mkldnn+FLAGS_nopir", dict(enable_mkldnn=True)),
    ("v5-mobile mkldnn+run_mode", dict(run_mode="mkldnn")),
]
BASE = dict(lang="ch", device="cpu", use_doc_orientation_classify=False,
            use_doc_unwarping=False, use_textline_orientation=False,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec")

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
        print(f"[OK] {name}: init={t_init:.1f}s ocr={dt:.1f}s lines={len(texts)} sample={''.join(texts)[:30]}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:100]}")

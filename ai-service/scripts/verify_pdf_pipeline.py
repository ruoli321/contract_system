# ═══════════════════════════════════════════════════════════════════
# verify_pdf_pipeline.py — 三份测试合同 PDF 全管线验证 + 评估报告数据生成
#
# 验证对象：pdf_parser 页级混合判定（2026-09 升级）
#   文字版 → text    / 扫描版 → scanned / 混合版 → mixed（页级分流）
#
# 用法（容器内，依赖齐全）:
#   docker cp test_pdfs/. contract-ai:/app/test_pdfs/
#   docker exec contract-ai python /app/scripts/verify_pdf_pipeline.py
# 输出:
#   /app/reports/pdf_pipeline_verification_YYYYMMDD.json（宿主机 reports/ 同步）
# ═══════════════════════════════════════════════════════════════════
import json
import os
import sys
import time

sys.path.insert(0, "/app")
from app.services.pdf_parser import PdfProcessor  # noqa: E402

CASES = [
    ("采购合同_文字版.pdf", "text"),
    ("采购合同_扫描版.pdf", "scanned"),
    ("采购合同_混合版.pdf", "mixed"),
]
# 关键字段抽查：编号/金额/双方/附件标题（混合版附件走 OCR，单独校验）
KEY_FIELDS_COMMON = ["CG-2026-0912-001", "582,000", "北京华信科技", "上海联创贸易"]
KEY_FIELDS_MIXED_EXTRA = ["产品配置清单", "到货验收单"]


def main():
    proc = PdfProcessor(ocr_engine="paddleocr", enable_table_detect=False)
    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "environment": ("Docker contract-ai / PaddleOCR 3.x PP-OCRv5_mobile "
                        "/ enable_mkldnn=False"),
        "files": [],
    }
    all_pass = True
    for name, expect in CASES:
        with open(f"/app/test_pdfs/{name}", "rb") as f:
            data = f.read()
        r = proc.extract_text(data)

        keys = list(KEY_FIELDS_COMMON)
        if name == "采购合同_混合版.pdf":
            keys += KEY_FIELDS_MIXED_EXTRA
        hits = [k for k in keys if k in r.text]

        item = {
            "file": name,
            "expected_type": expect,
            "pdf_type": r.pdf_type,
            "type_match": r.pdf_type == expect,
            "page_classes": r.metadata.get("page_classes", []),
            "pages": [
                {
                    "page_num": p.page_num,
                    "is_ocr": p.is_ocr,
                    "chars": p.text_layer_chars,
                    "preview": p.text[:24].replace("\n", " "),
                }
                for p in r.pages
            ],
            "metadata": {
                k: v for k, v in r.metadata.items() if k != "page_classes"
            },
            "total_chars": len(r.text),
            "has_nul": "\x00" in r.text,
            "key_fields_hit": hits,
            "key_fields_total": len(keys),
        }
        ok = (item["type_match"] and not item["has_nul"]
              and len(hits) == len(keys))
        item["verdict"] = "PASS" if ok else "FAIL"
        all_pass = all_pass and ok
        out["files"].append(item)
    out["overall"] = "ALL PASS" if all_pass else "HAS FAILURES"

    out_path = ("/app/reports/pdf_pipeline_verification_"
                + time.strftime("%Y%m%d") + ".json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("overall:", out["overall"])
    print("json:", out_path)
    for it in out["files"]:
        print(it["file"], "|", it["pdf_type"], "|", it["verdict"], "|",
              it["total_chars"], "字符 |",
              "命中", len(it["key_fields_hit"]), "/", it["key_fields_total"])


if __name__ == "__main__":
    main()

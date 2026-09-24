# ═══════════════════════════════════════════════════════════════════
# 本地轻量烟测：不依赖 Docker / PaddleOCR
# 验证范围：三信号探测 PageProbe、text_layered 判定、可疑页仲裁、
#           文字管线 fitz 主引擎、quality 块（missing_pages 等新字段）
# ═══════════════════════════════════════════════════════════════════
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ai-service/

from app.services.pdf_parser import PdfProcessor, EncryptedPdfError  # noqa: E402

PDF_DIR = Path(__file__).resolve().parent.parent.parent / "test_pdfs"


def main() -> int:
    proc = PdfProcessor(enable_table_detect=False)
    failures = []

    for pdf in sorted(PDF_DIR.glob("*.pdf")):
        data = pdf.read_bytes()
        result = proc.extract_text(data)
        quality = result.quality or {}
        meta = result.metadata or {}
        probes = meta.get("page_probes") or []
        print(f"\n=== {pdf.name} ===")
        print(f"  doc_type      = {result.pdf_type}")
        print(f"  pages         = {len(result.pages)}, chars = {len(result.text)}")
        print(f"  quality.score = {quality.get('score')}, ok = {quality.get('ok')}")
        print(f"  missing_pages = {meta.get('missing_pages')}, rescued = {meta.get('rescued_pages')}")
        for p in probes[:5]:
            print(f"  probe p{p['page_num']}: chars={p['chars']} readable={p['readable_ratio']:.2f} img_ratio={p['image_ratio']:.2f}")

        # ── 断言 ──
        if not result.text:
            failures.append(f"{pdf.name}: 文本为空")
        if result.pdf_type not in ("text", "text_layered", "scanned", "mixed"):
            failures.append(f"{pdf.name}: doc_type 非法 {result.pdf_type}")
        for p in probes:
            if p["chars"] < 0 or not (0.0 <= p["readable_ratio"] <= 1.0) or not (0.0 <= p["image_ratio"] <= 1.0):
                failures.append(f"{pdf.name}: probe 信号越界 {p}")
                break

    # ═══ 纯函数单元级验证（无 OCR 依赖）═══
    from app.services.pdf_parser import PageProbe

    # 1. 纠错词典
    stats = {"corrections": []}
    fixed = PdfProcessor._apply_glossary("签定时间: 甲万帐户", page_num=1, conf=0.6, block_stats=stats)
    print("\n[glossary]", fixed, "| corrections =", [(c['from'], c['to']) for c in stats["corrections"]])
    if fixed != "签订时间: 甲方账户":
        failures.append(f"glossary 纠错结果异常: {fixed}")

    # 2. 三信号类型判定
    cases = [
        (PageProbe(1, 500, 0.99, 0.10, 0.2), "text"),
        (PageProbe(1, 300, 0.98, 0.90, 0.9), "text_layered"),   # 大图+文本层
        (PageProbe(1, 5, 0.50, 0.95, 0.95), "scanned"),         # 大图无文本层
    ]
    cls = proc._classes_from_probes([c for c, _ in cases])
    print("[classes]", cls)
    for got, (_, want) in zip(cls, cases):
        if got != want:
            failures.append(f"类型判定错误: got={got} want={want}")

    # 3. 可疑页仲裁
    suspect_cases = [
        (PageProbe(1, 5, 0.9, 0.0, 0.0), "text", True),      # 空页
        (PageProbe(1, 200, 0.3, 0.0, 0.0), "text", True),    # 乱码页
        (PageProbe(1, 500, 0.99, 0.0, 0.0), "text", False),  # 正常页
    ]
    for probe, klass, want in suspect_cases:
        got = proc._page_is_suspect(probe, klass)
        if got != want:
            failures.append(f"可疑仲裁错误: chars={probe.chars} readable={probe.readable_ratio} got={got} want={want}")
    print("[suspect]", "ok")

    # 4. 阅读顺序（两行三块：y 相近的块聚成一行）
    items = [
        ("甲方：张三", 0.9, (10, 10, 100, 20)),
        ("乙方：李四", 0.9, (120, 10, 210, 20)),   # 同一行右侧
        ("第一条 付款", 0.9, (10, 60, 150, 70)),   # 下一行
    ]
    out = PdfProcessor._reading_order_text(items)
    print("[reading]", out.replace("\n", "\\n"))
    if out != "甲方：张三 乙方：李四\n\n第一条 付款":  # 行内空格拼接 + 段落间空行
        failures.append(f"阅读顺序拼接异常: {out!r}")

    print("\n" + ("=" * 50))
    if failures:
        print("SMOKE FAIL:")
        for f in failures:
            print("  ✗", f)
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

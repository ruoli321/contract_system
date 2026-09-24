# ═══════════════════════════════════════════════════════════════════
# 两套 OCR 方案准确率对比评估（同一批样本，实证数据）
#
# 方案 A（基线）：PaddleOCR 裸识别——无图像预处理、无版面后处理、无纠错
# 方案 B（产品管线）：纠偏+去噪预处理 → OCR → 阅读顺序/段落合并 →
#                     印章区块标记 → 合同词典纠错
#
# 样本构造：文字版 PDF 自带 ground truth 文本层 → fitz 渲染 200dpi
#           得到"扫描模拟图"（GT 与图像来自同一内容，天然配对）；
#           可选增强：旋转（模拟歪斜扫描）+ 高斯噪声（模拟脏污）
# 指标：CER（字符错误率）= Levenshtein(GT, OCR) / len(GT)，越小越好
#
# 用法（容器内）：
#   python scripts/eval_ocr_pipeline.py <文字版PDF...> [--max-pages 3] \
#       [--rotate 0 2 3.5] [--noise 0.05]
# ═══════════════════════════════════════════════════════════════════
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # /app

import cv2
import fitz
import numpy as np

from app.services.pdf_parser import PdfProcessor


def normalize(s: str) -> str:
    """归一化：去除全部空白——GT 的换行排版与 OCR 行拼接不可比，只比内容"""
    return "".join(s.split())


def levenshtein(a: str, b: str) -> int:
    """编辑距离（滚动数组，O(min(m,n)) 空间）"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(gt: str, hyp: str) -> float:
    if not gt:
        return 1.0 if hyp else 0.0
    return levenshtein(gt, hyp) / len(gt)


def baseline_run(proc: PdfProcessor, img, page_num: int) -> str:
    """方案 A：裸 OCR——直接 predict，模型输出顺序 join，无任何后处理"""
    with proc._ocr_lock:
        raw = proc._ocr.predict(img)
    lines = []
    for res in raw or []:
        texts = PdfProcessor._result_field(res, "rec_texts")
        if texts:
            lines.extend(texts)
    return "\n".join(lines)


def full_run(proc: PdfProcessor, img, page_num: int) -> str:
    """方案 B：产品管线——预处理（纠偏+去噪）→ OCR → 版面后处理+纠错"""
    pre = proc._preprocess_image(img)
    text, _, _ = proc._paddleocr_run(pre, page_num)
    return text


def augment(img, rotate_deg: float = 0.0, noise: float = 0.0):
    """扫描缺陷模拟：旋转（歪斜）+ 高斯噪声（脏污）"""
    out = img
    if rotate_deg:
        h, w = out.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2, h / 2), rotate_deg, 1.0)
        out = cv2.warpAffine(out, m, (w, h), flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_REPLICATE)
    if noise:
        g = np.random.normal(0, noise * 255, out.shape).astype(np.float32)
        out = np.clip(out.astype(np.float32) + g, 0, 255).astype(np.uint8)
    return out


def render_page(page, dpi: int = 200):
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 3:
        return np.ascontiguousarray(img[:, :, ::-1])  # RGB→BGR
    if pix.n == 1:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def main():
    ap = argparse.ArgumentParser(description="两套 OCR 方案 CER 对比")
    ap.add_argument("pdfs", nargs="+", help="文字版 PDF（自带文本层作 GT）")
    ap.add_argument("--max-pages", type=int, default=3, help="每份 PDF 最多取前 N 页")
    ap.add_argument("--rotate", type=float, nargs="*", default=[0.0, 2.0, 3.5],
                    help="旋转增强角度列表（°），0=不旋转")
    ap.add_argument("--noise", type=float, default=0.05,
                    help="高斯噪声强度（0~1，0 禁用；旋转场景叠加脏污）")
    args = ap.parse_args()
    np.random.seed(42)

    proc = PdfProcessor(ocr_engine="paddleocr", enable_table_detect=False)
    if not proc._init_ocr():
        print("❌ OCR 引擎初始化失败")
        sys.exit(1)

    # results[scene] = [(name, cer_base, cer_full, t_base, t_full)]
    results: dict = {}
    for pdf_path in args.pdfs:
        doc = fitz.open(pdf_path)
        name = Path(pdf_path).name
        for rot in args.rotate:
            scene = f"旋转{rot:g}°" + ("+噪声" if (rot and args.noise) else "")
            bucket = results.setdefault(scene, [])
            for pno in range(min(args.max_pages, len(doc))):
                page = doc[pno]
                gt = page.get_text() or ""
                if len(gt.strip()) < 50:
                    continue  # 无有效文本层的页没有 GT，跳过
                img = render_page(page)
                img = augment(img, rot, args.noise if rot else 0.0)
                gt_n = normalize(gt)

                t0 = time.time()
                hyp_base = baseline_run(proc, img, pno + 1)
                t_base = time.time() - t0
                t0 = time.time()
                hyp_full = full_run(proc, img, pno + 1)
                t_full = time.time() - t0

                bucket.append((f"{name}#p{pno + 1}",
                               cer(gt_n, normalize(hyp_base)),
                               cer(gt_n, normalize(hyp_full)),
                               t_base, t_full))
                print(f"  [{scene}] {name}#p{pno + 1}: "
                      f"基线 {bucket[-1][1]:.4f} vs 管线 {bucket[-1][2]:.4f}")
        doc.close()

    # ── 汇总表 ──
    print("\n" + "=" * 78)
    print(f"{'场景':<12}{'样本':>4}{'基线CER':>10}{'管线CER':>10}{'相对改善':>10}{'耗时/页 基线→管线':>22}")
    print("-" * 78)
    for scene, rows in results.items():
        n = len(rows)
        cb = sum(r[1] for r in rows) / n
        cf = sum(r[2] for r in rows) / n
        gain = (cb - cf) / cb * 100 if cb > 0 else 0.0
        tb = sum(r[3] for r in rows) / n
        tf = sum(r[4] for r in rows) / n
        print(f"{scene:<12}{n:>4}{cb:>9.2%}{cf:>9.2%}{gain:>+9.1f}%{tb:>10.1f}s→{tf:.1f}s")
    print("=" * 78)
    print("说明：CER 越低越好。改善主要来自：纠偏（旋转场景）+ 去噪（脏污场景）+")
    print("      阅读顺序还原与词典纠错（对 CER 影响小，主要提升下游字段提取质量）。")


if __name__ == "__main__":
    main()

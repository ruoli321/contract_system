# ═══════════════════════════════════════════════════════════════════
# 生成 3 个仿真合同样本 PDF（用于人工测试上传/提取/OCR 全链路）
#
#   1. 文字版_办公设备采购合同_CG2025-018.pdf   （有文本层，pdfplumber 直抽）
#   2. 扫描版_房屋租赁合同_ZL2025-007.pdf       （纯图片层，走 OCR 管线）
#   3. 扫描版_软件开发服务合同_RW2025-021.pdf   （纯图片层，走 OCR 管线）
#
# 仿真要素：
#   - 正文仿宋 11pt / 条款标题黑体 / 居中大标题，A4 版式 + 页脚页码
#   - 双栏签署区 + 法定代表人签字线 + 签署日期
#   - PIL 绘制红色圆形公章（外圈 + 五角星 + 弧形公司名 + "合同专用章"）
#   - 扫描版：200dpi 渲染 → 光照不均 + 高斯噪声 + 轻微模糊 + 纸张发灰
#             + 边缘暗角 + 0.4~1.4° 随机倾斜 + JPEG 压缩伪影（q 58~75）
#             → 纯图片 PDF（无文本层），元数据伪装成佳能一体机扫描输出
#
# 运行：容器内 python scripts/generate_manual_test_pdfs.py
# 输出：/test_pdfs/manual/（宿主机 test_pdfs/manual/）
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import io
import math
import os
import re
import zlib
from datetime import datetime
from pathlib import Path

import cv2
import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── 路径解析：容器优先 /test_pdfs，宿主机回退到仓库 test_pdfs ──
if Path("/test_pdfs").exists():
    TEST_PDFS = Path("/test_pdfs")
else:
    TEST_PDFS = Path(__file__).resolve().parents[2] / "test_pdfs"
OUT_DIR = TEST_PDFS / "manual"
FONT_DIR = TEST_PDFS / ".fonts"

# 输出目录：优先 test_pdfs/manual；容器内该挂载只读时自动回退到
# /app/reports/contract_samples（宿主机 ai-service/reports/contract_samples）
try:
    (TEST_PDFS / ".write_test").write_text("x")
    (TEST_PDFS / ".write_test").unlink()
except OSError:
    OUT_DIR = Path(os.environ.get("CONTRACT_PDF_OUT")
                   or "/app/reports/contract_samples")

F_FANG = str(FONT_DIR / "simfang.ttf")   # 仿宋：正文
F_HEI = str(FONT_DIR / "simhei.ttf")     # 黑体：标题/条款头
F_SONG = str(FONT_DIR / "simsun.ttc")    # 宋体：公章文字

# A4 尺寸（pt）与版心
PAGE_W, PAGE_H = 595.28, 841.89
MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 75, 75, 72, 68
TEXT_W = PAGE_W - MARGIN_L - MARGIN_R

# 测量用 Font 对象（度量与子集字体一致，供排版断行使用）
FANG = fitz.Font(fontfile=F_FANG)
HEI = fitz.Font(fontfile=F_HEI)


def prepare_subfonts(all_text: str):
    """
    按实际用字生成字体子集（fontTools），避免全量嵌入 10MB+ 中文字体。

    子集文件写入输出目录（持久挂载），成功后重定向 F_FANG/F_HEI；
    fontTools 不可用时回退为嵌入完整字体。
    """
    global F_FANG, F_HEI
    try:
        from fontTools import subset
    except ImportError:
        print("⚠️ fontTools 未安装，嵌入完整字体（文件较大）")
        return
    extra = ("0123456789ABCXYZabcxyz.-,;:!?()[]{}<>％％‰×÷§ "
             "，。、；：？！（）【】《》「」『』“”‘’—…·￥％")
    for src, dst_name in ((F_FANG, "simfang_sub.ttf"), (F_HEI, "simhei_sub.ttf")):
        dst = OUT_DIR / dst_name
        opts = subset.Options()
        opts.layout_features = []       # 合同排版不需要 OpenType 特性
        opts.name_IDs = [1, 2, 3, 4, 6]
        font = subset.load_font(src, opts)
        s = subset.Subsetter(opts)
        s.populate(text=all_text + extra)
        s.subset(font)
        subset.save_font(font, str(dst), opts)
        kb = dst.stat().st_size / 1024
        print(f"🔤 字体子集 {dst_name}: {kb:.0f} KB")
    F_FANG = str(OUT_DIR / "simfang_sub.ttf")
    F_HEI = str(OUT_DIR / "simhei_sub.ttf")


# ═══════════════════════════════════════════════════════════════════
# 一、红色公章绘制（PIL，RGBA PNG，可预旋转）
# ═══════════════════════════════════════════════════════════════════
def make_stamp_png(company: str, size: int = 560, angle: float = 0.0) -> bytes:
    """绘制圆形公章 PNG。company 沿上弧排布，底部横排「合同专用章」。"""
    S = 4  # 4x 超采样抗锯齿，最后缩小
    W = size * S
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    RED = (198, 32, 32, 235)
    cx = cy = W // 2
    R = W // 2 - 8 * S

    # 外圈（公章双线感：粗外圈）
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=RED, width=7 * S)

    # 五角星（居中）
    r_out, r_in = R * 0.30, R * 0.115
    pts = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, fill=RED)

    # 弧形公司名（沿上弧，逐字旋转）
    n = len(company)
    font = ImageFont.truetype(F_SONG, int(R * 0.22), index=0)
    arc_span = min(150, 26 * n)          # 名字长则摊开角度更大
    start_deg = -arc_span / 2
    r_text = R - R * 0.17
    for i, ch in enumerate(company):
        deg = start_deg + arc_span * (i / max(n - 1, 1))
        rad = math.radians(deg)
        px = cx + r_text * math.sin(rad)
        py = cy - r_text * math.cos(rad)
        # 单字渲染到小画布再旋转粘贴
        cw = int(R * 0.26)
        tile = Image.new("RGBA", (cw, cw), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        bbox = td.textbbox((0, 0), ch, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        td.text((cw / 2 - tw / 2 - bbox[0], cw / 2 - th / 2 - bbox[1]),
                ch, font=font, fill=RED)
        tile = tile.rotate(deg, resample=Image.BICUBIC,
                           expand=False, center=(cw / 2, cw / 2))
        img.alpha_composite(tile, (int(px - cw / 2), int(py - cw / 2)))

    # 底部横排「合同专用章」
    font2 = ImageFont.truetype(F_SONG, int(R * 0.20), index=0)
    label = "合同专用章"
    bbox = d.textbbox((0, 0), label, font=font2)
    tw = bbox[2] - bbox[0]
    d.text((cx - tw / 2, cy + R * 0.52), label, font=font2, fill=RED)

    img = img.resize((size, size), Image.LANCZOS)
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════
# 二、文字版排版引擎（段落流 → 自动分页 → fitz 绘制）
# ═══════════════════════════════════════════════════════════════════
STYLES = {
    #             字体  字号  行距   对齐        段前  首行缩进
    "title":   (HEI,  16.5, 26,  "center",   10,  0),
    "meta":    (FANG, 11,   19,  "center",    4,  0),
    "party":   (FANG, 11,   19,  "left",      0,  0),
    "head":    (HEI,  11.5, 21,  "left",      7,  0),
    "body":    (FANG, 11,   19,  "left",      0,  22),
    "blank":   (FANG, 6,    10,  "left",      0,  0),
}


_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.,%‰]*|.")


def _wrap(text: str, font: fitz.Font, size: float, width: float,
          first_indent: float) -> list[str]:
    """按实测字宽断行：中文可逐字断行，ASCII 数字/单词串保持完整不断开"""
    lines, cur, cur_w = [], "", first_indent
    for tok in _TOKEN.findall(text):
        w = font.text_length(tok, size)
        if cur_w + w > width and cur:
            lines.append(cur)
            cur, cur_w = tok, w
        else:
            cur += tok
            cur_w += w
    if cur:
        lines.append(cur)
    return lines or [""]


def layout(doc_spec: dict) -> list[list[tuple]]:
    """把合同规格转成分页后的行盒列表 pages[page][line] = (text, style, x_offset)"""
    # 1) 展开为 (text, style) 流
    flow: list[tuple] = []
    for kind, text in doc_spec["blocks"]:
        if kind == "blank":
            flow.append(("", "blank"))
            continue
        font, size, leading, align, space_before, indent = STYLES[kind]
        for ln in _wrap(text, font, size, TEXT_W, indent):
            flow.append((ln, kind))
        if kind in ("title", "meta", "head"):
            flow.append(("", "blank"))
    # 2) 签署块（双栏）
    sig_lines = doc_spec["signature"]
    flow.append(("__SIGN__", "sign"))

    # 3) 分页
    pages, page, y = [], [], MARGIN_T
    for text, kind in flow:
        if text == "__SIGN__":
            need = len(sig_lines["left"]) * 19 + 26
            if y + need > PAGE_H - MARGIN_B:
                pages.append(page)
                page, y = [], MARGIN_T
            page.append(("__SIGN__", "sign", 0))
            y += need
            continue
        font, size, leading, align, space_before, indent = STYLES[kind]
        if space_before and page:
            y += space_before * 0.5
        if y + leading > PAGE_H - MARGIN_B:
            pages.append(page)
            page, y = [], MARGIN_T
        page.append((text, kind, indent))
        y += leading
    pages.append(page)
    return pages


def draw_text_pdf(spec: dict, out_path: Path):
    """生成文字版 PDF（矢量文本 + 公章图片）"""
    pages = layout(spec)
    doc = fitz.open()
    n = len(pages)
    for pno, page_lines in enumerate(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_font(fontname="FS", fontfile=F_FANG)
        page.insert_font(fontname="HEI", fontfile=F_HEI)

        # 页眉（第 2 页起：右上角灰色小字）
        if pno > 0:
            hdr = f"{spec['title']}　{spec['code']}"
            page.insert_text((PAGE_W - MARGIN_R - FANG.text_length(hdr, 8.5),
                              40), hdr, fontsize=8.5, fontname="FS",
                             color=(0.45, 0.45, 0.45))

        y = MARGIN_T
        for text, kind, indent in page_lines:
            if text == "__SIGN__":
                y = _draw_signature(page, spec["signature"], y)
                continue
            font, size, leading, align, sb, ind = STYLES[kind]
            fname = "HEI" if font is HEI else "FS"
            if kind == "title":
                x = (PAGE_W - font.text_length(text, size)) / 2
            elif kind == "meta":
                x = (PAGE_W - font.text_length(text, size)) / 2
            else:
                x = MARGIN_L + indent
            color = (0, 0, 0)  # 标题与正文均为黑色（真实合同惯例）
            page.insert_text((x, y + size), text, fontsize=size,
                             fontname=fname, color=color)
            y += leading

        # 页脚：第 X 页 共 Y 页
        foot = f"第 {pno + 1} 页　共 {n} 页"
        page.insert_text(((PAGE_W - FANG.text_length(foot, 9)) / 2, PAGE_H - 36),
                         foot, fontsize=9, fontname="FS",
                         color=(0.35, 0.35, 0.35))

    # 公章盖在末页签署区
    _stamp_last_page(doc, pages, spec)

    doc.set_metadata({
        "title": spec["title"], "author": spec["party_a"]["name"],
        "subject": f"合同编号 {spec['code']}", "creator": "合同管理系统",
    })
    doc.save(str(out_path), garbage=3, deflate=True)
    doc.close()


def _draw_signature(page, sig: dict, y: float) -> float:
    """双栏签署区：左甲方 / 右乙方，返回结束 y"""
    x_left, x_right = MARGIN_L, PAGE_W / 2 + 40
    size, leading = 10.5, 19
    page.insert_text((x_left, y + size), "（以下无正文，为签署栏）",
                     fontsize=9.5, fontname="FS", color=(0.3, 0.3, 0.3))
    y += leading
    for l, r in zip(sig["left"], sig["right"]):
        page.insert_text((x_left, y + size), l, fontsize=size, fontname="FS")
        page.insert_text((x_right, y + size), r, fontsize=size, fontname="FS")
        y += leading
    return y


def _stamp_last_page(doc, pages, spec: dict):
    """在末页签署区两栏上方各盖一枚红章"""
    page = doc[len(pages) - 1]
    sig = spec["signature"]
    n_lines = len(sig["left"])
    sig_top = PAGE_H - MARGIN_B - (n_lines * 19 + 26)  # 近似签署区顶部 y
    cy = min(max(sig_top + 30, 120), PAGE_H - 160)
    D = 96
    for cx, angle in ((MARGIN_L + 105, -5), (PAGE_W / 2 + 145, 7)):
        png = make_stamp_png(spec["stamp_name"], size=560, angle=angle)
        page.insert_image(fitz.Rect(cx - D / 2, cy - D / 2, cx + D / 2, cy + D / 2),
                          stream=png, overlay=True)


# ═══════════════════════════════════════════════════════════════════
# 三、扫描效果管线（渲染 → cv2 做旧 → JPEG → 纯图片 PDF）
# ═══════════════════════════════════════════════════════════════════
def page_to_bgr(page: fitz.Page, dpi: int = 200) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    return img


def apply_scan_effects(bgr: np.ndarray, rng: np.random.RandomState) -> bytes:
    """模拟办公彩色扫描件：光照不均 + 噪声 + 模糊 + 纸底微灰 + 倾斜 + 暗角 + JPEG

    保持彩色（红章扫描后仍为红色，更接近真实办公扫描仪彩色模式输出）。
    """
    h, w = bgr.shape[:2]
    img = bgr.astype(np.float32)

    # 1) 光照不均（低频随机亮度场 0.88~1.05）
    g = rng.uniform(0.88, 1.05, size=(9, 7)).astype(np.float32)
    light = cv2.resize(g, (w, h), interpolation=cv2.INTER_LINEAR)
    img *= light[..., None]

    # 2) 轻微失焦
    img = cv2.GaussianBlur(img, (0, 0), 0.7)

    # 3) 纸底微灰：压高光、抬黑位（背景 255 → ~238）
    img = np.clip((img - 12) * 1.015 + 10, 0, 255)

    # 4) 传感器噪声（逐通道）
    img += rng.normal(0, 5.5, img.shape)

    # 5) 随机倾斜 ±(0.4~1.4)°（扫描进纸歪斜），边缘用暖白填充
    ang = rng.uniform(0.4, 1.4) * rng.choice([-1, 1])
    M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
    img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC,
                         borderValue=(226, 234, 240))  # BGR 暖白

    # 6) 边缘暗角（扫描仪盖板阴影）
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.minimum.reduce([xx, yy, w - 1 - xx, h - 1 - yy]).astype(np.float32)
    shade = np.clip(dist / (0.025 * min(w, h)), 0, 1) * 0.07 + 0.93
    img *= shade[..., None]

    # 7) 轻微暖色偏（纸张荧光，B 略降 R 略升）
    img[..., 0] -= 3
    img[..., 2] += 2

    out = np.clip(img, 0, 255).astype(np.uint8)
    q = int(rng.randint(58, 76))
    ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, q])
    assert ok
    return jpg.tobytes()


def make_scanned_pdf(spec: dict, out_path: Path, dpi: int = 200):
    """文字版 → 逐页扫描化 → 纯图片 PDF（无文本层）"""
    # 先在内存里生成文字版 PDF，再逐页渲染 + 扫描化
    buf = io.BytesIO()
    _build_text_doc(spec, buf)
    src = fitz.open(stream=buf.getvalue(), filetype="pdf")
    rng = np.random.RandomState(abs(hash(spec["code"])) % (2 ** 31))

    doc = fitz.open()
    for page in src:
        bgr = page_to_bgr(page, dpi=dpi)
        jpg = apply_scan_effects(bgr, rng)
        p = doc.new_page(width=PAGE_W, height=PAGE_H)
        p.insert_image(p.rect, stream=jpg)

    doc.set_metadata({
        "title": spec["title"], "author": spec["party_a"]["name"],
        "subject": f"合同编号 {spec['code']}",
        "creator": "", "producer": "Canon iR-ADV C5550 ScanGear",
    })
    doc.save(str(out_path), deflate=True)
    doc.close()
    src.close()


def _build_text_doc(spec: dict, buf: io.BytesIO):
    """内部复用 draw_text_pdf 的逻辑，输出到字节流（不写公章角度随机种子）"""
    pages = layout(spec)
    doc = fitz.open()
    n = len(pages)
    for pno, page_lines in enumerate(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_font(fontname="FS", fontfile=F_FANG)
        page.insert_font(fontname="HEI", fontfile=F_HEI)
        if pno > 0:
            hdr = f"{spec['title']}　{spec['code']}"
            page.insert_text((PAGE_W - MARGIN_R - FANG.text_length(hdr, 8.5),
                              40), hdr, fontsize=8.5, fontname="FS",
                             color=(0.45, 0.45, 0.45))
        y = MARGIN_T
        for text, kind, indent in page_lines:
            if text == "__SIGN__":
                y = _draw_signature(page, spec["signature"], y)
                continue
            font, size, leading, align, sb, ind = STYLES[kind]
            fname = "HEI" if font is HEI else "FS"
            if kind in ("title", "meta"):
                x = (PAGE_W - font.text_length(text, size)) / 2
            else:
                x = MARGIN_L + indent
            color = (0, 0, 0)  # 标题与正文均为黑色（真实合同惯例）
            page.insert_text((x, y + size), text, fontsize=size,
                             fontname=fname, color=color)
            y += leading
        foot = f"第 {pno + 1} 页　共 {n} 页"
        page.insert_text(((PAGE_W - FANG.text_length(foot, 9)) / 2, PAGE_H - 36),
                         foot, fontsize=9, fontname="FS",
                         color=(0.35, 0.35, 0.35))
    _stamp_last_page(doc, pages, spec)
    doc.set_metadata({"title": spec["title"], "author": spec["party_a"]["name"],
                      "subject": f"合同编号 {spec['code']}",
                      "creator": "合同管理系统"})
    doc.save(buf, garbage=3, deflate=True)
    doc.close()


# ═══════════════════════════════════════════════════════════════════
# 四、三份合同内容
# ═══════════════════════════════════════════════════════════════════
def sig_block(a_label: str, b_label: str, a_rep: str, b_rep: str,
              a_tel: str, b_tel: str, date: str) -> dict:
    pad = "　" * 2
    return {
        "left": [
            f"甲方（盖章）：{a_label}",
            f"法定代表人或授权代表（签字）：{a_rep}",
            f"联系电话：{a_tel}",
            f"签署日期：{date}",
        ],
        "right": [
            f"乙方（盖章）：{b_label}",
            f"法定代表人或授权代表（签字）：{b_rep}",
            f"联系电话：{b_tel}",
            f"签署日期：{date}",
        ],
    }


def contract_purchase() -> dict:
    return {
        "code": "CG2025-018", "title": "办公设备采购合同",
        "stamp_name": "北京华宇科技有限公司合同专用章",
        "party_a": {"name": "北京华宇科技有限公司"},
        "blocks": [
            ("meta", "合同编号：CG2025-018"),
            ("meta", "签订地点：北京市海淀区　　签订时间：2025年6月18日"),
            ("title", "办公设备采购合同"),
            ("party", "甲方（采购方）：北京华宇科技有限公司"),
            ("body", "法定代表人：李国强　　统一社会信用代码：91110108MA01Y7K2X8"),
            ("body", "地址：北京市海淀区中关村大街27号创新大厦14层"),
            ("party", "乙方（供货方）：深圳市腾达办公设备有限公司"),
            ("body", "法定代表人：王秀兰　　统一社会信用代码：91440300MA5DK3M96F"),
            ("body", "地址：深圳市南山区科技南十二路18号金蝶软件园B座501"),
            ("body", "根据《中华人民共和国民法典》及相关法律法规的规定，甲乙双方本着平等自愿、诚实信用的原则，就甲方向乙方采购办公设备事宜协商一致，达成如下条款，以资共同遵守："),
            ("head", "第一条　采购标的及技术要求"),
            ("body", "1. 甲方向乙方采购以下办公设备：台式计算机120台，单价4,200元，小计504,000元；激光打印机30台，单价2,800元，小计84,000元；智能会议平板8台，单价12,500元，小计100,000元；机架式服务器2台，单价89,000元，小计178,000元。"),
            ("body", "2. 上述货物均应为全新原装正品，具体品牌、型号及技术参数以双方共同确认的《设备配置清单》（附件一）为准，附件与本合同具有同等法律效力。"),
            ("head", "第二条　合同总价"),
            ("body", "本合同总价为人民币捌拾陆万陆仟元整（￥866,000.00）。该总价已包含设备价款、包装费、运输费、装卸费、安装调试费、培训费及13%增值税税金，乙方应向甲方开具等额增值税专用发票。"),
            ("head", "第三条　付款方式"),
            ("body", "1. 预付款：本合同签订后10个工作日内，甲方向乙方支付合同总价的30%，计人民币259,800元；"),
            ("body", "2. 到货款：全部设备送达甲方指定地点并经甲方验收合格后15个工作日内，甲方向乙方支付合同总价的60%，计人民币519,600元；"),
            ("body", "3. 质保金：剩余10%计人民币86,600元作为质量保证金，自全部设备验收合格之日起满12个月且无质量问题后10个工作日内，甲方一次性付清。"),
            ("body", "乙方指定收款账户：开户银行为中国工商银行深圳科技园支行，银行账号为4000021909200123456，户名为深圳市腾达办公设备有限公司。乙方收款账户变更须提前5个工作日书面通知甲方，否则损失自负。"),
            ("head", "第四条　交付与验收"),
            ("body", "1. 乙方应于2025年7月20日前将全部设备运抵甲方指定交货地点（北京市海淀区中关村大街27号创新大厦），货物运输、保险及卸货费用由乙方承担。"),
            ("body", "2. 货物到达后5个工作日内，双方共同开箱清点并按出厂标准及附件一约定配置进行验收；验收不合格的，乙方应在7日内无偿更换或修复，由此产生的费用由乙方承担。"),
            ("head", "第五条　质量保证与售后"),
            ("body", "设备免费质保期自验收合格之日起不少于36个月。质保期内发生非人为损坏的质量问题，乙方应免费维修或更换；乙方应在接到甲方报修通知后48小时内响应，一般故障72小时内解决。"),
            ("head", "第六条　违约责任"),
            ("body", "1. 乙方逾期交付设备的，每逾期一日，应按合同总价的0.5‰向甲方支付违约金；逾期交付超过15日的，甲方有权解除本合同，乙方还应支付合同总价10%的违约金。"),
            ("body", "2. 甲方逾期付款的，每逾期一日，应按应付未付金额的0.5‰向乙方支付违约金。"),
            ("head", "第七条　争议解决"),
            ("body", "因履行本合同发生的争议，双方应友好协商解决；协商不成的，任何一方均有权向甲方所在地有管辖权的人民法院提起诉讼。"),
            ("head", "第八条　合同生效及其他"),
            ("body", "本合同一式肆份，甲乙双方各执贰份，具有同等法律效力，自双方签字并加盖公章之日起生效。本合同未尽事宜，由双方协商签订补充协议解决。"),
        ],
        "signature": sig_block("北京华宇科技有限公司", "深圳市腾达办公设备有限公司",
                               "＿＿＿＿＿＿", "＿＿＿＿＿＿",
                               "010-82366018", "0755-86120966",
                               "2025 年 6 月 18 日"),
    }


def contract_lease() -> dict:
    return {
        "code": "ZL2025-007", "title": "房屋租赁合同",
        "stamp_name": "北京城建置业发展有限公司合同专用章",
        "party_a": {"name": "北京城建置业发展有限公司"},
        "blocks": [
            ("meta", "合同编号：ZL2025-007"),
            ("meta", "签订地点：北京市朝阳区　　签订时间：2025年3月10日"),
            ("title", "房屋租赁合同"),
            ("party", "出租方（甲方）：北京城建置业发展有限公司"),
            ("body", "法定代表人：张建国　　统一社会信用代码：91110105MA004X9Q7B"),
            ("body", "地址：北京市朝阳区北辰东路8号汇欣大厦A座21层"),
            ("party", "承租方（乙方）：北京明德教育科技有限公司"),
            ("body", "法定代表人：陈晓峰　　统一社会信用代码：91110105MA01P6RT3D"),
            ("body", "地址：北京市海淀区学院路30号科教楼5层"),
            ("body", "根据《中华人民共和国民法典》及有关法律、法规的规定，甲乙双方在平等、自愿、协商一致的基础上，就房屋租赁事宜订立本合同。"),
            ("head", "第一条　租赁房屋基本情况"),
            ("body", "甲方将位于北京市朝阳区望京街10号方恒国际中心B座12层1201至1208室的房屋（建筑面积860.55平方米，以下简称「该房屋」）出租给乙方，用于办公经营。该房屋产权证明文件复印件见附件一。"),
            ("head", "第二条　租赁期限"),
            ("body", "租赁期限自2025年4月1日起至2028年3月31日止，共计3年。租赁期满，乙方有意继续承租的，应提前90日向甲方提出书面续租申请，在同等条件下乙方享有优先承租权。"),
            ("head", "第三条　租金标准及支付方式"),
            ("body", "1. 该房屋月租金为人民币肆万伍仟元整（￥45,000.00），年租金总额为人民币伍拾肆万元整（￥540,000.00），租赁期内甲方不得单方上调租金。"),
            ("body", "2. 租金按季度支付，采用「押一付三」方式，乙方应于每季度首月5日前将当季租金足额支付至甲方指定账户。"),
            ("body", "3. 甲方指定收款账户：开户银行为中国建设银行北京朝阳支行，银行账号为11050189563600001234，户名为北京城建置业发展有限公司。"),
            ("head", "第四条　押金"),
            ("body", "本合同签订之日起5日内，乙方应向甲方支付押金人民币壹拾万元整（￥100,000.00）。租赁期满或合同解除后，乙方结清应由乙方承担的各项费用且该房屋及附属设施验收无损坏的，甲方应于10日内将押金无息退还乙方。"),
            ("head", "第五条　相关费用的承担"),
            ("body", "租赁期内该房屋发生的水费、电费、燃气费、物业管理费、网络通讯费及乙方经营产生税费由乙方自行承担；房产税、租赁备案税费依法由甲方承担。"),
            ("head", "第六条　房屋交付与返还"),
            ("body", "1. 甲方应于2025年4月1日前将该房屋以适租状态交付乙方，双方共同签署《房屋交接单》。"),
            ("body", "2. 租赁期满或合同解除后3日内，乙方应将该房屋恢复至交接状态（自然损耗除外）并返还甲方；逾期返还的，按日租金的两倍支付房屋占用费。"),
            ("head", "第七条　装修与使用"),
            ("body", "乙方对该房屋进行装修装饰的，装修方案须事先报甲方及物业管理单位书面同意，不得损坏房屋主体承重结构；未经甲方书面同意，乙方不得将该房屋全部或部分转租给第三方。"),
            ("head", "第八条　违约责任"),
            ("body", "1. 乙方逾期支付租金超过15日的，每逾期一日按欠付金额的1‰向甲方支付违约金；逾期超过30日的，甲方有权单方解除合同并没收押金。"),
            ("body", "2. 甲方逾期交付该房屋的，每逾期一日按月租金的2%向乙方支付违约金；逾期超过30日的，乙方有权解除合同，甲方应双倍返还押金。"),
            ("head", "第九条　争议解决"),
            ("body", "因履行本合同发生的争议，双方应协商解决；协商不成的，提请北京仲裁委员会按照该会仲裁规则进行仲裁，仲裁裁决是终局的，对双方均有约束力。"),
            ("head", "第十条　合同生效"),
            ("body", "本合同一式叁份，甲方执壹份，乙方执贰份，自双方签字并加盖公章之日起生效。"),
        ],
        "signature": sig_block("北京城建置业发展有限公司", "北京明德教育科技有限公司",
                               "＿＿＿＿＿＿", "＿＿＿＿＿＿",
                               "010-64912256", "010-62324567",
                               "2025 年 3 月 10 日"),
    }


def contract_service() -> dict:
    return {
        "code": "RW2025-021", "title": "软件开发服务合同",
        "stamp_name": "北京睿智软创科技有限公司合同专用章",
        "party_a": {"name": "上海锦程国际贸易有限公司"},
        "blocks": [
            ("meta", "合同编号：RW2025-021"),
            ("meta", "签订地点：上海市浦东新区　　签订时间：2025年8月5日"),
            ("title", "软件开发服务合同"),
            ("party", "委托方（甲方）：上海锦程国际贸易有限公司"),
            ("body", "法定代表人：周文斌　　统一社会信用代码：91310115MA1K4T7L0C"),
            ("body", "地址：上海市浦东新区世纪大道88号金茂大厦3506室"),
            ("party", "受托方（乙方）：北京睿智软创科技有限公司"),
            ("body", "法定代表人：赵一鸣　　统一社会信用代码：91110108MA02B8N5X1"),
            ("body", "地址：北京市海淀区西二旗中路33号领秀科技园D座8层"),
            ("body", "依据《中华人民共和国民法典》关于技术合同的相关规定，甲方委托乙方开发「智慧供应链管理系统」，双方经友好协商，就开发服务事宜达成如下协议："),
            ("head", "第一条　项目内容与开发范围"),
            ("body", "1. 乙方为甲方定制开发智慧供应链管理系统（以下简称「本系统」），开发范围包括：供应商管理、采购订单管理、库存预警、物流跟踪、对账结算、数据可视化看板共六大业务模块，具体功能需求以双方共同确认的《需求规格说明书》（附件一）为准。"),
            ("body", "2. 乙方应组织具备相应资质与项目经验的技术团队实施本项目，项目经理及核心开发人员在项目期内未经甲方书面同意不得更换。"),
            ("head", "第二条　开发周期与里程碑"),
            ("body", "1. 本项目自2025年8月20日正式启动，乙方应于2026年2月28日前完成系统开发、内部测试并部署至甲方生产环境。"),
            ("body", "2. 关键里程碑：2025年10月31日前完成需求确认与原型设计；2025年12月31日前完成核心功能开发并提测；2026年2月10日前完成系统集成测试并提交甲方组织验收。"),
            ("head", "第三条　合同价款"),
            ("body", "本项目开发费用总额为人民币壹佰贰拾捌万元整（￥1,280,000.00），该价款已包含乙方实施本项目的全部人工成本、差旅费、培训费及6%增值税，甲方无须另行支付任何费用。"),
            ("head", "第四条　付款方式"),
            ("body", "1. 第一期：本合同生效后10个工作日内，甲方向乙方支付合同总价的30%，计人民币384,000元；"),
            ("body", "2. 第二期：本系统部署上线并通过甲方组织的上线验收后10个工作日内，甲方支付合同总价的40%，计人民币512,000元；"),
            ("body", "3. 第三期：系统终验合格且连续稳定运行满3个月后10个工作日内，甲方支付剩余30%，计人民币384,000元。"),
            ("body", "乙方指定收款账户：开户银行为招商银行北京西二旗支行，银行账号为6225880137761234，户名为北京睿智软创科技有限公司。"),
            ("head", "第五条　验收标准与方式"),
            ("body", "1. 验收依据为附件一《需求规格说明书》及附件二《测试验收方案》。系统功能实现率不低于98%，核心业务接口平均响应时间不超过500毫秒，年度系统可用性不低于99.9%。"),
            ("body", "2. 甲方应在收到乙方书面验收申请后10个工作日内组织验收；甲方逾期未组织验收且未提出书面异议的，视为验收合格。"),
            ("head", "第六条　知识产权"),
            ("body", "1. 本系统交付并验收合格后，其全部知识产权（包括但不限于源代码、技术文档、数据库设计）归甲方所有。"),
            ("body", "2. 乙方在开发过程中使用的通用组件、开发框架的知识产权仍归乙方所有，乙方授予甲方在本系统范围内永久免费的使用许可。"),
            ("body", "3. 乙方保证交付成果不侵犯任何第三方知识产权，否则由乙方承担全部法律责任及甲方因此遭受的损失。"),
            ("head", "第七条　保密条款"),
            ("body", "双方对履行本合同过程中知悉的对方商业秘密、技术秘密及经营数据负有保密义务，保密期限自本合同签订之日起5年，不因本合同的解除或终止而失效。违反保密义务的，应向对方支付合同总价20%的违约金。"),
            ("head", "第八条　售后服务"),
            ("body", "乙方提供自终验合格之日起12个月的免费运维服务，包括缺陷修复、每月不少于1次的系统巡检及7×24小时故障响应；一般故障2小时内响应、24小时内解决，重大故障4小时内响应、8小时内恢复。"),
            ("head", "第九条　违约责任"),
            ("body", "1. 乙方逾期交付的，每逾期一日按合同总价的1‰向甲方支付违约金；逾期超过30日的，甲方有权解除合同，乙方应退还甲方已支付款项的50%并交付已完成成果。"),
            ("body", "2. 甲方逾期付款的，每逾期一日按应付未付金额的1‰向乙方支付违约金；项目工期相应顺延。"),
            ("head", "第十条　争议解决"),
            ("body", "因本合同引起的或与本合同有关的任何争议，双方应首先友好协商解决；协商不成的，任何一方均可向被告住所地有管辖权的人民法院提起诉讼。"),
            ("head", "第十一条　合同生效"),
            ("body", "本合同一式肆份，甲乙双方各执贰份，自双方法定代表人或授权代表签字并加盖公章之日起生效。附件一、附件二为本合同不可分割的组成部分，与本合同正文具有同等法律效力。"),
        ],
        "signature": sig_block("上海锦程国际贸易有限公司", "北京睿智软创科技有限公司",
                               "＿＿＿＿＿＿", "＿＿＿＿＿＿",
                               "021-58821666", "010-82053399",
                               "2025 年 8 月 5 日"),
    }


# ═══════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    specs = [
        ("文字版_办公设备采购合同_CG2025-018.pdf", contract_purchase(), False),
        ("扫描版_房屋租赁合同_ZL2025-007.pdf", contract_lease(), True),
        ("扫描版_软件开发服务合同_RW2025-021.pdf", contract_service(), True),
    ]
    # 汇总全部用字 → 生成字体子集（避免嵌入 10MB 全量中文字体）
    all_text = "".join(t for _, spec, _ in specs for _, t in spec["blocks"] if t)
    all_text += "".join("".join(sp["signature"]["left"] + sp["signature"]["right"])
                        for _, sp, _ in specs)
    prepare_subfonts(all_text)

    for fname, spec, scanned in specs:
        out = OUT_DIR / fname
        if scanned:
            make_scanned_pdf(spec, out, dpi=200)
        else:
            draw_text_pdf(spec, out)
        kb = out.stat().st_size / 1024
        # 自检：文本层字符数（文字版应 >500，扫描版应为 0）
        d = fitz.open(str(out))
        chars = sum(len(p.get_text().strip()) for p in d)
        print(f"✅ {fname} | {len(d)} 页 | {kb:.0f} KB | 文本层 {chars} 字")
        d.close()
    print(f"\n输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()

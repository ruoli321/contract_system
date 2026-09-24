# ═══════════════════════════════════════════════════════════════════
# 生成三份测试合同 PDF，用于验证 pdf_parser 页级混合判定
#
#   采购合同_文字版.pdf — 全部文字页（预期 pdf_type = text）
#   采购合同_扫描版.pdf — 全部整页图片（预期 pdf_type = scanned）
#   采购合同_混合版.pdf — 前3页文字 + 2页扫描附件 + 1页带章签署页
#                         （预期 pdf_type = mixed，页级分流）
#
# 用法:
#   cd ai-service && python scripts/generate_contract_samples.py
# 输出:
#   test_pdfs/采购合同_文字版.pdf
#   test_pdfs/采购合同_扫描版.pdf
#   test_pdfs/采购合同_混合版.pdf
# ═══════════════════════════════════════════════════════════════════
import os
import sys

import fitz

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)          # ai-service/
OUTPUT_DIR = os.path.join(ROOT_DIR, "test_pdfs")

# 优先用系统黑体（更接近真实合同观感），不存在则退回 fitz 内置中文字体
FONT_PATH = r"C:\Windows\Fonts\simhei.ttf"


# ─────────────────────────────────────────────────────────────────────
# 合同正文（每页一个行列表，行宽控制在 ~34 字以内防止越界）
# ─────────────────────────────────────────────────────────────────────
PAGE_1 = [
    "采购合同",
    "",
    "合同编号：CG-2026-0912-001",
    "签订地点：北京市海淀区",
    "签订日期：2026年9月12日",
    "",
    "甲方（采购方）：北京华信科技有限公司",
    "统一社会信用代码：91110108MA01X2Y34Z",
    "住所地：北京市海淀区中关村大街1号",
    "法定代表人：张伟",
    "联系电话：010-8888 6666",
    "",
    "乙方（供货方）：上海联创贸易有限公司",
    "统一社会信用代码：91310115MA1K3L45M6",
    "住所地：上海市浦东新区博云路2号",
    "法定代表人：李娜",
    "联系电话：021-6666 8888",
    "",
    "根据《中华人民共和国民法典》及相关法律法规的规定，",
    "甲乙双方本着平等自愿、诚实信用的原则，就甲方向乙方",
    "采购办公设备及软件事宜协商一致，订立本合同，",
    "以资双方共同遵守。",
]

PAGE_2 = [
    "第一条 采购标的",
    "乙方向甲方提供以下产品：",
    "1. 高性能服务器 5 台，单价人民币 86,000 元；",
    "2. 企业级交换机 4 台，单价人民币 9,500 元；",
    "3. 正版操作系统软件 10 套，单价人民币 3,200 元。",
    "产品品牌、配置及技术参数详见附件一《产品配置清单》。",
    "",
    "第二条 合同金额",
    "合同总金额为人民币伍拾捌万贰仟元整（¥582,000.00），",
    "为含税价格（增值税率13%）。除本合同另有约定外，",
    "该金额包含设备价款、包装费、运输费、保险费、",
    "安装调试费及税费等全部费用，乙方不得以任何理由",
    "要求增加费用。",
    "",
    "第三条 付款方式",
    "3.1 预付款：本合同生效后5个工作日内，甲方向乙方支付",
    "合同总额的30%，计人民币壹拾柒万肆仟陆佰元整；",
    "3.2 到货款：全部货物交付且验收合格后10个工作日内，",
    "甲方支付合同总额的60%，计人民币叁拾肆万玖仟贰佰元整；",
    "3.3 质保金：剩余10%计伍万捌仟贰佰元整作为质量保证金，",
    "质保期满且无质量问题后15个工作日内无息支付。",
    "3.4 乙方指定收款账户：招商银行上海张江支行，",
    "账号1219 0233 8801 056，户名上海联创贸易有限公司。",
]

PAGE_3 = [
    "第四条 交付与验收",
    "4.1 乙方应于合同生效后20日内将全部货物送达甲方指定地点",
    "并完成安装调试，交付地点：北京市海淀区中关村大街1号；",
    "4.2 甲方应在收货后7个工作日内按附件一进行验收，验收合格",
    "的签署书面验收单；验收不合格的，乙方应在5个工作日内",
    "予以更换或修复，由此产生的费用由乙方承担。",
    "",
    "第五条 质量保证与售后服务",
    "5.1 乙方保证所供产品为全新原装正品，质保期为验收合格",
    "之日起3年；",
    "5.2 质保期内发生非人为损坏的，乙方应在接到通知后48小时",
    "内响应，并免费维修或更换。",
    "",
    "第六条 违约责任",
    "6.1 乙方逾期交付的，每逾期一日按合同总额的万分之五向",
    "甲方支付违约金；逾期超过15日的，甲方有权解除合同并",
    "要求乙方赔偿损失；",
    "6.2 甲方逾期付款的，每逾期一日按应付未付金额的万分之五",
    "向乙方支付违约金；",
    "6.3 任何一方违约给对方造成损失的，应依法承担赔偿责任。",
]

PAGE_4 = [
    "第七条 保密条款",
    "双方对在履行本合同过程中知悉的对方商业秘密及技术资料",
    "负有保密义务，未经对方书面同意不得向任何第三方披露。",
    "本条款在本合同终止后继续有效。",
    "",
    "第八条 争议解决",
    "本合同履行过程中发生争议的，双方应友好协商解决；",
    "协商不成的，任何一方均可向甲方所在地有管辖权的",
    "人民法院提起诉讼。",
    "",
    "第九条 其他约定",
    "9.1 本合同未尽事宜，双方另行签订补充协议，补充协议",
    "与本合同具有同等法律效力；",
    "9.2 本合同一式肆份，甲乙双方各执贰份，自双方签字盖章",
    "之日起生效；",
    "9.3 本合同附件为合同不可分割的组成部分。",
    "",
    "（以下无正文，为签署页）",
]

PAGE_SIGN = [
    "（本页为签署页，无正文）",
    "",
    "",
    "甲方（盖章）：北京华信科技有限公司",
    "",
    "乙方（盖章）：上海联创贸易有限公司",
    "",
    "法定代表人/授权代表（签字）：＿＿＿＿＿＿",
    "",
    "签订日期：2026年9月12日",
]

ANNEX_1 = [
    "附件一 产品配置清单",
    "",
    "序号  产品名称          规格/配置              数量  单价(元)",
    "1    高性能服务器      2×Xeon 4314/128G/4T    5台   86,000",
    "2    企业级交换机      48口千兆+4口万兆       4台   9,500",
    "3    操作系统软件      Windows Server 2022    10套  3,200",
    "",
    "备注：以上价格为含税单价，币种为人民币。",
    "清单经甲乙双方确认后作为验收依据，不得单方变更。",
]

ANNEX_2 = [
    "附件二 到货验收单",
    "",
    "验收日期：＿＿＿＿年＿＿月＿＿日",
    "验收地点：北京市海淀区中关村大街1号甲方机房",
    "",
    "验收内容：",
    "□ 外包装完好，无破损、受潮痕迹",
    "□ 设备序列号与装箱单一致",
    "□ 开机自检正常，配置与附件一相符",
    "□ 软件授权证书及许可文件齐全",
    "",
    "验收结论：□ 合格    □ 不合格（说明：＿＿＿＿＿＿）",
    "",
    "甲方验收人（签字）：＿＿＿＿＿＿",
    "乙方交付人（签字）：＿＿＿＿＿＿",
]


# ─────────────────────────────────────────────────────────────────────
# 绘制工具
# ─────────────────────────────────────────────────────────────────────
def _draw_lines(page, lines, title=False):
    """在页面上逐行绘制文本，自动选择可用中文字体"""
    fontname = "china-s"
    kwargs = {}
    if os.path.exists(FONT_PATH):
        fontname = "simhei"
        kwargs["fontfile"] = FONT_PATH

    y = 72
    for idx, line in enumerate(lines):
        if not line:  # 空行
            y += 14
            continue
        if title and idx == 0:
            page.insert_text((250, y), line, fontname=fontname,
                             fontsize=16, **kwargs)
            y += 36
            continue
        page.insert_text((72, y), line, fontname=fontname,
                         fontsize=11.5, **kwargs)
        y += 21


def _draw_stamp(page, x, y, company):
    """画一枚红色圆形合同章（占页面面积约1%，用于面积占比仲裁测试）"""
    r = 42
    page.draw_circle(fitz.Point(x, y), r, color=(0.75, 0, 0), width=1.6)
    page.insert_text(fitz.Point(x - 34, y - 12), company[:10],
                     fontname="china-s", fontsize=8.5, color=(0.75, 0, 0))
    page.insert_text(fitz.Point(x - 24, y + 2), company[10:],
                     fontname="china-s", fontsize=8.5, color=(0.75, 0, 0))
    page.insert_text(fitz.Point(x - 22, y + 16), "合同专用章",
                     fontname="china-s", fontsize=9, color=(0.75, 0, 0))


def _new_doc_with_pages(page_lines_list, stamps=None):
    """按行列表逐页绘制文字版 PDF"""
    doc = fitz.open()
    for idx, lines in enumerate(page_lines_list):
        page = doc.new_page()
        _draw_lines(page, lines, title=(idx == 0))
        if stamps and idx in stamps:
            for x, y, company in stamps[idx]:
                _draw_stamp(page, x, y, company)
    return doc


def _render_to_jpegs(doc, dpi=150, quality=75):
    """把 PDF 每页渲染为 JPEG 字节（模拟扫描件，有损压缩控制体积）"""
    jpegs = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        try:
            jpegs.append(pix.tobytes("jpeg", jpg_quality=quality))
        except (TypeError, ValueError):   # 旧版 fitz 不支持 jpg_quality
            jpegs.append(pix.tobytes("jpeg"))
    return jpegs


def _new_doc_from_images(images):
    """把 JPEG 字节逐页贴成整页图片 PDF（扫描件形态）"""
    doc = fitz.open()
    for img in images:
        page = doc.new_page()
        page.insert_image(page.rect, stream=img)
    return doc


# ─────────────────────────────────────────────────────────────────────
# 三份合同的组装
# ─────────────────────────────────────────────────────────────────────
def _maybe_subset(doc):
    """子集化嵌入字体：simhei 全量嵌入约 10MB，子集化后仅保留用到的字形"""
    try:
        doc.subset_fonts()
    except Exception:
        pass  # fontTools 不可用时跳过，仅影响文件体积


def make_text_pdf():
    """纯文字版：5 页全部文字，签署页带小章（占比 <15%，仍应判文字页）"""
    doc = _new_doc_with_pages(
        [PAGE_1, PAGE_2, PAGE_3, PAGE_4, PAGE_SIGN],
        stamps={4: [(150, 700, "北京华信科技有限公司"),
                    (430, 700, "上海联创贸易有限公司")]},
    )
    _maybe_subset(doc)
    return doc.tobytes()


def make_scanned_pdf():
    """纯扫描版：与文字版同内容，但每页都是整页图片"""
    text_doc = _new_doc_with_pages(
        [PAGE_1, PAGE_2, PAGE_3, PAGE_4, PAGE_SIGN],
        stamps={4: [(150, 700, "北京华信科技有限公司"),
                    (430, 700, "上海联创贸易有限公司")]},
    )
    jpegs = _render_to_jpegs(text_doc)
    text_doc.close()
    return _new_doc_from_images(jpegs).tobytes()


def make_mixed_pdf():
    """混合版：前3页文字正文 + 2页扫描附件 + 1页带章签署页（文字）"""
    # 附件渲染成扫描图
    annex_doc = _new_doc_with_pages([ANNEX_1, ANNEX_2])
    annex_jpegs = _render_to_jpegs(annex_doc)
    annex_doc.close()

    doc = fitz.open()
    # 页 1-3：文字正文
    for lines in (PAGE_1, PAGE_2, PAGE_3):
        page = doc.new_page()
        _draw_lines(page, lines, title=(lines is PAGE_1))
    # 页 4-5：扫描附件（整页图片）
    for img in annex_jpegs:
        page = doc.new_page()
        page.insert_image(page.rect, stream=img)
    # 页 6：文字签署页 + 两枚小章
    page = doc.new_page()
    _draw_lines(page, PAGE_SIGN)
    _draw_stamp(page, 150, 700, "北京华信科技有限公司")
    _draw_stamp(page, 430, 700, "上海联创贸易有限公司")
    _maybe_subset(doc)
    return doc.tobytes()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    samples = {
        "采购合同_文字版.pdf": make_text_pdf(),
        "采购合同_扫描版.pdf": make_scanned_pdf(),
        "采购合同_混合版.pdf": make_mixed_pdf(),
    }
    for name, data in samples.items():
        path = os.path.join(OUTPUT_DIR, name)
        with open(path, "wb") as f:
            f.write(data)
        print(f"✅ 已生成: {path}  ({len(data)/1024:.0f} KB)")


if __name__ == "__main__":
    main()

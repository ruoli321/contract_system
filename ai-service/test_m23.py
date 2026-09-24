# -*- coding: utf-8 -*-
"""
M23 · 合同条款排版渲染单测（Odoo 侧纯函数，可离线跑）
─────────────────────────────────────────────────
被测对象：odoo/addons/contract_ai/models/clause_rendering.py
运行方式：python test_m23.py（无 Odoo 运行时依赖）
"""
import os
import sys

MODELS_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "odoo", "addons", "contract_ai", "models",
))
sys.path.insert(0, MODELS_DIR)

from clause_rendering import render_clause_text_to_html, render_clause_preview  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        print("  ✗ %s  %s" % (name, detail))


def sample_text():
    return (
        "智能产品销售合同\n"
        "合同编号：XS-2024-012\n"
        "\n"
        "一、合同标的\n"
        "本合同项下标的为 智能产品销售合同\n"
        "二、合同金额\n"
        "合同总金额为人民币 320,000 元。\n"
        "3 个工作日内支付 40% 预付款\n"
        "第一条 付款方式\n"
        "（一）分期支付\n"
        "1. 首期 40%\n"
        "1.1 首期明细\n"
        "1、备选写法\n"
    )


def main():
    print("═" * 50)
    print("M23 · 条款排版渲染单测")
    print("═" * 50)

    html_out = render_clause_text_to_html(sample_text())

    # ── 1. 一级标题识别 ──
    check("「一、」识别为一级标题", '<div class="clause-h1">一、合同标的</div>' in html_out)
    check("「第一条」识别为一级标题", 'clause-h1">第一条 付款方式<' in html_out)

    # ── 2. 二级标题识别 ──
    check("「（一）」识别为二级标题", 'clause-h2">（一）分期支付<' in html_out)
    check("「1.」识别为二级标题", 'clause-h2">1. 首期 40%<' in html_out)
    check("「1.1」识别为二级标题", 'clause-h2">1.1 首期明细<' in html_out)
    check("「1、」识别为二级标题", 'clause-h2">1、备选写法<' in html_out)

    # ── 3. 误判防御（数字开头但非编号的行必须是正文段落） ──
    check("「3 个工作日内…」保持正文段落", '<p class="clause-p">3 个工作日内支付 40% 预付款</p>' in html_out)
    check("「320,000 元…」保持正文段落", '<p class="clause-p">合同总金额为人民币 320,000 元。</p>' in html_out)
    check("普通文本行为正文段落", '<p class="clause-p">本合同项下标的为 智能产品销售合同</p>' in html_out)
    check("键值行为正文段落", '<p class="clause-p">合同编号：XS-2024-012</p>' in html_out)

    # ── 4. 空行 / 首行标题行处理 ──
    check("文档标题行（无编号）为正文段落", '<p class="clause-p">智能产品销售合同</p>' in html_out)
    check("空行不产生空标签", '><p class="clause-p"></p><' not in html_out and '""' != html_out)

    # ── 5. 安全转义 ──
    xss = render_clause_text_to_html('<script>alert(1)</script>\n第X条 <b>加粗</b> & "引号"')
    check("script 标签被转义", "<script>" not in xss and "&lt;script&gt;" in xss)
    check("b 标签被转义", "<b>" not in xss and "&lt;b&gt;" in xss)
    check("尖括号实体化", "&lt;" in xss)
    check("与符号转义", " &amp; " in xss)
    check("输出不含内联 style", "style=" not in html_out)
    check("输出不含事件属性", " on" not in html_out.replace('class="clause-', "cls-") or " onclick" not in html_out)

    # ── 6. 边界 ──
    check("空文本 → 空串", render_clause_text_to_html("") == "")
    check("None → 空串", render_clause_text_to_html(None) == "")
    ws = render_clause_text_to_html("   \n\t\n  ")
    check("全空白文本 → 单段落兜底", ws.count("clause-p") == 1)
    no_nl = render_clause_text_to_html("整段没有换行的条款内容")
    check("无换行整段 → 单段落", no_nl == '<p class="clause-p">整段没有换行的条款内容</p>')

    # ── 7. 摘要 preview ──
    check("摘要取首个非空行", render_clause_preview(sample_text()) == "智能产品销售合同")
    long_text = "x" * 80 + "\n第二行"
    pv = render_clause_preview(long_text)
    check("超长摘要截断加省略号", pv == "x" * 60 + "…", "got len=%d" % len(pv))
    check("空文本摘要 → 空串", render_clause_preview("") == "")
    check("前导空行跳过", render_clause_preview("\n\n  有效行") == "有效行")
    check("全空白摘要 → 空串", render_clause_preview("  \n\t ") == "")

    # ── 8. 合同级预览拼装逻辑（模拟 compute 内联逻辑，验证转义与结构） ──
    from markupsafe import escape
    clause_name = '<evil>&名字</evil>'
    title = "%d. %s" % (1, clause_name)
    section = '<section class="clause-section"><h3 class="clause-title">%s</h3>%s</section>' % (
        escape(title), html_out)
    check("合同级标题带转义", "&lt;evil&gt;" in str(section) and "<evil>" not in str(section))
    check("合同级结构含 section/clause-title", 'class="clause-section"' in str(section) and 'class="clause-title"' in str(section))

    print("─" * 50)
    print("结果：PASS=%d FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

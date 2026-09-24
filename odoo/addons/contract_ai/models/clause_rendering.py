# ════════════════════════════════════════════════
# 合同条款纯文本 → 结构化 HTML 排版渲染（M23）
# ──────────────────────────────────────────────
# 设计目标：条款正文在 Odoo 界面（预览面板/编辑对话框）中呈现
# 报刊级排版：标题层级加粗、正文段落首行缩进、段间距统一。
#
# 原则：
#   1. 纯函数模块（不依赖 odoo），便于离线单测（test_m23.py）
#   2. 数据层不动：content 仍为 Text 存储，渲染只发生在展示层
#   3. 安全：所有文本经 html.escape 转义后再拼 HTML，防注入
#   4. 仅输出 class 类样式（不内联 style），统一由静态 CSS 承载，
#      保证浏览器/设备间渲染一致（详见 static/src/css/clause_display.css）
# ════════════════════════════════════════════════
import html
import re

# 一级标题：第X条 / 第X章 / 一、二、（中文顿号编号）
_CLAUSE_H1_RE = re.compile(
    r"^(第[0-9一二三四五六七八九十百千两]+[条章]|[一二三四五六七八九十]+、)"
)
# 二级标题：（一）/ (1) / 1、 / 1. / 1.1 等编号项
# 注意：纯数字后跟空格不算编号（如「3 个工作日内支付」），必须紧跟顿号/点
_CLAUSE_H2_RE = re.compile(
    r"^([（(][0-9一二三四五六七八九十]+[）)]|\d+[、.．]|\d+(?:\.\d+)+)"
)


def render_clause_text_to_html(text):
    """把条款纯文本渲染为结构化 HTML。

    规则：
    - 空行仅作分隔（段间距由 CSS margin 承载，不输出空标签）
    - 「第X条 / 第X章 / 一、」→ div.clause-h1（一级标题，加粗）
    - 「（一） / 1. / 1、 / 1.1」→ div.clause-h2（二级标题，加粗缩进）
    - 其余行为正文段落 p.clause-p（首行缩进 2 字符，CSS 控制）
    - 全部文本 html.escape 转义，仅含 class，不含内联 style/事件属性
    """
    if not text:
        return ""
    blocks = []
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        esc = html.escape(line, quote=False)
        if _CLAUSE_H1_RE.match(line):
            blocks.append('<div class="clause-h1">%s</div>' % esc)
        elif _CLAUSE_H2_RE.match(line):
            blocks.append('<div class="clause-h2">%s</div>' % esc)
        else:
            blocks.append('<p class="clause-p">%s</p>' % esc)
    if not blocks:
        # 整段无换行或全空白：按单段落输出
        blocks.append('<p class="clause-p">%s</p>' % html.escape(str(text).strip(), quote=False))
    return "".join(blocks)


def render_clause_preview(text, max_length=60):
    """条款正文单行摘要（列表视图列展示用）。

    取首个非空行，超长截断加省略号。
    """
    if not text:
        return ""
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line:
            if len(line) > max_length:
                return line[:max_length] + "…"
            return line
    return ""

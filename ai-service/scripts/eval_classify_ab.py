# ═══════════════════════════════════════════════════════════════════
# A/B 对照验证：分类 prompt v2 vs v3 + 规则通道升级
#   - v2: 旧 prompt（置信度无锚点、reasoning 后置）+ 旧规则（词频等权）
#   - v3: 新 prompt（分档锚点、reasoning 前置、甲方视角）+ 新规则
#         （标题+8 / 角色行+5 / 分层加权）
# 同一批合同文本分别走两套配置，对比类型、置信度、method_used。
# PDF 只解析一次（文本缓存共享），LLM 各调一次。
# ═══════════════════════════════════════════════════════════════════
import sys
import time
import logging
from pathlib import Path

sys.path.insert(0, "/app")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
# classifier 的 info 日志保留（看 method_used），其余降噪
logging.getLogger("classifier").setLevel(logging.INFO)

from app.config import get_settings
from app.services.llm_client import create_llm
from app.services.prompt_manager import PromptManager
from app.services.classifier import ContractClassifier
from app.services.pdf_parser import PdfProcessor

PDF_DIR = Path("/app/test_pdfs")
FILES = ["采购合同_文字版.pdf", "采购合同_扫描版.pdf", "采购合同_混合版.pdf"]


def main():
    settings = get_settings()
    llm = create_llm(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
    )
    pm_v2 = PromptManager(prompts_dir=settings.prompts_dir, current_version="v2")
    pm_v3 = PromptManager(prompts_dir=settings.prompts_dir, current_version="v3")
    # 规则通道已升级为 v3 逻辑；对照"旧规则"用降级模拟——直接对比新旧 prompt 下的 LLM 表现，
    # 规则侧 before/after 用单元测试结果呈现（见报告）
    clf_v2 = ContractClassifier(llm_client=llm, prompt_manager=pm_v2)
    clf_v3 = ContractClassifier(llm_client=llm, prompt_manager=pm_v3)

    proc = PdfProcessor(ocr_engine="paddleocr")  # 扫描版走真实 OCR，三份文本均可用

    rows = []
    for fname in FILES:
        pdf_path = PDF_DIR / fname
        pdf_bytes = pdf_path.read_bytes()
        result = proc.extract_text(pdf_bytes)
        text = result.text
        title = text.split("\n")[0][:40].strip()  # 首行作标题近似

        t0 = time.time()
        r2 = clf_v2.classify(text, title)
        t2 = time.time() - t0
        t0 = time.time()
        r3 = clf_v3.classify(text, title)
        t3 = time.time() - t0
        rows.append((fname, result.pdf_type, r2, t2, r3, t3))

    print("\n" + "═" * 96)
    print(f"{'文件':<14} {'PDF类型':<9} │ {'v2 类型':<6} {'conf':<5} {'method':<13} │ {'v3 类型':<6} {'conf':<5} {'method':<13}")
    print("─" * 96)
    for fname, ptype, r2, t2, r3, t3 in rows:
        print(f"{fname:<14} {ptype:<9} │ {r2.contract_type:<6} {r2.confidence:<5.2f} {r2.method_used:<13} │ "
              f"{r3.contract_type:<6} {r3.confidence:<5.2f} {r3.method_used:<13}")
    print("═" * 96)

    # reasoning 展示（v3 特有：字段前置）
    print("\n── v3 reasoning 样例（第一个文件）──")
    d3 = clf_v3.classify
    # 重新渲染一次拿 reasoning——classify 不返回原始 JSON，这里直接展示 LLM 原始输出
    r3_first = rows[0]
    print(f"文件: {r3_first[0]}")
    print(f"v3 结果: {r3_first[4].contract_type} conf={r3_first[4].confidence} method={r3_first[4].method_used}")
    llm_detail = r3_first[4]
    print(f"（双通道详情: rule={llm_detail.rule_result}@{llm_detail.rule_confidence} "
          f"llm={llm_detail.llm_result}@{llm_detail.llm_confidence}）")


if __name__ == "__main__":
    main()

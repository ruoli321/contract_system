"""
M15 · 合同字段提取评估与报告模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

功能：
  1. 加载 examples/annotations/*.json 金标准标注
  2. 从标注合成模拟合同文本（无 PDF 时兜底）或调 PdfProcessor 读真实 PDF
  3. 分别跑 use_rag=True 和 use_rag=False 两组
  4. 逐字段对比（日期/金额语义等价 + 字符串包含关系）
  5. 构造问答对计算 RAG 指标（faithfulness + answer_relevancy）
  6. 生成 Markdown 报告 + JSON 原始数据

用法：
  # 有 LLM API Key（真实调用）
  python scripts/evaluate_m15.py

  # 离线模式（不调 LLM，从 annotation 直接拿 mock 预测值）
  python scripts/evaluate_m15.py --offline

  # 跳过 RAGAS（不装 ragas 时自动降级，也可手动指定）
  python scripts/evaluate_m15.py --no-ragas

  # 指定标注目录和 PDF 目录
  python scripts/evaluate_m15.py --annot-dir examples/annotations --pdf-dir examples/gold_contracts
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("m15_eval")

BASE_DIR = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════════
# 1. 评估字段定义（与 field_dict.json 保持一致）
# ═══════════════════════════════════════════════════════════════════
EVAL_FIELDS = [
    "contract_name",
    "contract_code",
    "partner_a",
    "partner_b",
    "amount",
    "amount_uppercase",
    "sign_date",
    "effective_date",
    "expire_date",
    "payment_terms",
    "breach_clause",
    "dispute_resolution",
]

# 字段中文名（用于报告表头）
FIELD_ZH = {
    "contract_name": "合同名称",
    "contract_code": "合同编号",
    "partner_a": "甲方",
    "partner_b": "乙方",
    "amount": "合同金额",
    "amount_uppercase": "金额大写",
    "sign_date": "签订日期",
    "effective_date": "生效日期",
    "expire_date": "到期日期",
    "payment_terms": "付款条款",
    "breach_clause": "违约责任",
    "dispute_resolution": "争议解决",
    "contract_type": "合同类型",
}

# 每个字段对应的自然语言问题（用于构造 RAGAS 问答对）
FIELD_QUESTIONS = {
    "contract_name": "这份合同的完整名称是什么？",
    "contract_code": "合同编号是什么？",
    "partner_a": "合同中的甲方是哪家公司？",
    "partner_b": "合同中的乙方是哪家公司？",
    "amount": "合同的总金额是多少？",
    "amount_uppercase": "合同金额的大写写法是什么？",
    "sign_date": "合同的签订日期是哪一天？",
    "effective_date": "合同的生效日期是哪一天？",
    "expire_date": "合同的到期或终止日期是哪一天？",
    "payment_terms": "合同中的付款方式和付款条件是什么？",
    "breach_clause": "合同中规定的违约责任有哪些？",
    "dispute_resolution": "合同中约定的争议解决方式是什么？",
    "contract_type": "这份合同属于哪一种类型？",
}


# ═══════════════════════════════════════════════════════════════════
# 2. 加载数据
# ═══════════════════════════════════════════════════════════════════
def load_annotations(annot_dir: Path) -> list[dict]:
    """扫描标注目录，加载所有金标准"""
    annotations = []
    if not annot_dir.exists():
        logger.error(f"标注目录不存在: {annot_dir}")
        return annotations

    for p in sorted(annot_dir.glob("*.json")):
        if p.name.startswith("_"):
            continue  # 跳过 _TEMPLATE.json 等说明文件
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            annotations.append(data)
        except Exception as e:
            logger.warning(f"  ⚠️  跳过损坏的标注 {p.name}: {e}")

    logger.info(f"📚 加载了 {len(annotations)} 份金标准标注来自 {annot_dir}")
    return annotations


def synthesize_contract_text(annot: dict) -> str:
    """
    从标注合成一段"合同文本"。
    没有真实 PDF 时兜底，保证 extractor 能从合成文本里抽到标注值。
    合成策略：每个字段一行，模拟真实合同的常见措辞。
    """
    lines = []
    cname = annot.get("contract_name") or f"{annot.get('contract_type', '合同')}"
    ccode = annot.get("contract_code")
    ctype = annot.get("contract_type", "")

    lines.append(f"《{cname}》")
    lines.append("")
    if ctype:
        lines.append(f"本合同为{ctype}，由以下双方于{annot.get('sign_date', '')}签订：")
    else:
        lines.append(f"本合同由以下双方于{annot.get('sign_date', '')}签订：")
    lines.append("")
    if annot.get("partner_a"):
        lines.append(f"甲方（需方）：{annot['partner_a']}")
    if annot.get("partner_b"):
        lines.append(f"乙方（供方）：{annot['partner_b']}")
    if ccode:
        lines.append(f"合同编号：{ccode}")
    lines.append("")

    amount = annot.get("amount")
    amt_up = annot.get("amount_uppercase")
    if amount is not None:
        lines.append(f"第一条  合同总金额：人民币 {amount} 元")
        if amt_up:
            lines.append(f"（大写：{amt_up}）")

    sign_date = annot.get("sign_date")
    eff_date = annot.get("effective_date")
    exp_date = annot.get("expire_date")
    if sign_date or eff_date or exp_date:
        lines.append("")
        lines.append("第二条  合同期限")
        if sign_date:
            lines.append(f"    本合同于{sign_date}签订。")
        if eff_date:
            lines.append(f"    自{eff_date}起生效。")
        if exp_date:
            lines.append(f"    至{exp_date}终止。")

    pt = annot.get("payment_terms")
    if pt:
        lines.append("")
        lines.append("第三条  付款方式")
        lines.append(f"    {pt}")

    bc = annot.get("breach_clause")
    if bc:
        lines.append("")
        lines.append("第四条  违约责任")
        lines.append(f"    {bc}")

    dr = annot.get("dispute_resolution")
    if dr:
        lines.append("")
        lines.append("第五条  争议解决")
        if dr == "诉讼":
            lines.append("    本合同争议应向人民法院提起诉讼。")
        elif dr == "仲裁":
            lines.append("    本合同争议应提交仲裁委员会仲裁。")
        elif dr == "协商":
            lines.append("    双方应首先友好协商解决争议。")
        else:
            lines.append(f"    {dr}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 3. 字段语义比较（核心评估逻辑）
# ═══════════════════════════════════════════════════════════════════
def _normalize_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip().lower().replace(" ", "").replace("　", "").replace("\t", "")


def _normalize_date(s: str) -> Optional[str]:
    """统一日期为 YYYY-MM-DD"""
    if not s:
        return None
    m = re.match(r"(\d{4})[-/年\.](\d{1,2})[-/月\.](\d{1,2})", s.strip())
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _normalize_amount(val: Any) -> Optional[float]:
    """金额归一化：支持数字、带逗号的字符串、带"万元"后缀"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("，", "")
    # 处理"万元""元"后缀
    if "万元" in s:
        s = s.replace("万元", "")
        try:
            return float(s) * 10000
        except ValueError:
            return None
    if "元" in s:
        s = s.replace("元", "")
    try:
        return float(s)
    except ValueError:
        return None


def field_match(pred: Any, gold: Any, field_name: str) -> bool:
    """
    判断字段是否语义等价。
    - 日期字段：统一 YYYY-MM-DD 后比较
    - 金额字段：数值差 < 2%
    - 大写金额：归一化字符串 + 模糊包含
    - 字符串字段：精确匹配 + 双向包含关系
    """
    # 都为空 → 匹配（确实没提到）
    if pred is None and gold is None:
        return True
    if pred is None or gold is None:
        return False

    pred_s = str(pred).strip()
    gold_s = str(gold).strip()

    if not pred_s and not gold_s:
        return True
    if not pred_s or not gold_s:
        return False

    # ── 日期字段 ──
    if field_name in ("sign_date", "effective_date", "expire_date"):
        np = _normalize_date(pred_s)
        ng = _normalize_date(gold_s)
        if np and ng:
            return np == ng
        return _normalize_str(pred_s) == _normalize_str(gold_s)

    # ── 金额字段 ──
    if field_name == "amount":
        fp = _normalize_amount(pred)
        fg = _normalize_amount(gold)
        if fp is not None and fg is not None:
            if fg == 0:
                return fp == 0
            return abs(fp - fg) / abs(fg) < 0.02  # 2% 容差
        # 退而求其次：字符串模糊匹配
        return _fuzzy_text_match(pred_s, gold_s)

    # ── 大写金额 ──
    if field_name == "amount_uppercase":
        return _fuzzy_text_match(pred_s, gold_s)

    # ── 合同类型（枚举字段）──
    if field_name == "contract_type":
        return _normalize_str(pred_s) == _normalize_str(gold_s)

    # ── 默认：包含关系 + 模糊匹配 ──
    return _fuzzy_text_match(pred_s, gold_s)


def _fuzzy_text_match(a: str, b: str) -> bool:
    """模糊文本匹配：归一化 + 双向包含"""
    na, nb = _normalize_str(a), _normalize_str(b)
    if not na or not nb:
        return na == nb == ""
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    # 数字部分匹配（金额场景）
    nums_a = re.findall(r"\d+\.?\d*", na)
    nums_b = re.findall(r"\d+\.?\d*", nb)
    if nums_a and nums_b and nums_a == nums_b:
        return True
    return False


def compare_all_fields(pred: dict, gold: dict, extra: Optional[dict] = None) -> dict:
    """
    对比一份合同的所有评估字段。
    返回 {"field_name": {"pred": ..., "gold": ..., "match": bool, "note": str}}
    extra: 额外上下文（比如 contract_type / id）用于日志
    """
    result = {}
    for field in EVAL_FIELDS:
        pred_val = pred.get(field) if isinstance(pred, dict) else None
        gold_val = gold.get(field)
        matched = field_match(pred_val, gold_val, field)
        result[field] = {
            "pred": _snippet(pred_val),
            "gold": _snippet(gold_val),
            "match": matched,
            "note": None,
        }
    return result


def _snippet(val: Any, limit: int = 80) -> Optional[str]:
    if val is None:
        return None
    s = str(val)
    return s[:limit] + ("..." if len(s) > limit else "")


# ═══════════════════════════════════════════════════════════════════
# 4. 运行提取（两组：RAG ON / RAG OFF）
# ═══════════════════════════════════════════════════════════════════
def load_services_or_mock(offline: bool = False):
    """
    初始化评估需要的服务。
    offline=True 时不初始化 LLM，返回 mock。
    """
    sys.path.insert(0, str(BASE_DIR))

    if offline:
        logger.info("🏷️  offline 模式：跳过 LLM 初始化，评估脚本直接从 annotation 拿 mock 预测值")
        return None, None

    try:
        from app.config import get_settings
        from app.services.llm_client import create_llm
        from app.services.pdf_parser import PdfProcessor
        from app.services.vector_store import VectorStore
        from app.services.rag_learner import RAGLearner
        from app.services.prompt_manager import PromptManager
        from app.services.classifier import ContractClassifier
        from app.services.extractor import ContractExtractor

        settings = get_settings()

        # 检查 API Key
        if not settings.llm_api_key:
            logger.warning("⚠️  LLM API Key 未配置，自动切换到 --offline 模式")
            return None, None

        llm = create_llm(
            provider=settings.llm_provider,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
        parser = PdfProcessor()
        vs = VectorStore(
            host=settings.chroma_host,
            port=settings.chroma_port,
            embedding_model=settings.embedding_model,
            embedding_dimension=settings.embedding_dimension,
        )
        pm = PromptManager(prompts_dir=settings.prompts_dir)
        classifier = ContractClassifier(llm_client=llm, prompt_manager=pm)
        rag_learner = RAGLearner(vector_store=vs)
        extractor = ContractExtractor(llm, prompt_manager=pm, rag_learner=rag_learner)

        logger.info("✅ 服务初始化成功：LLM + PdfProcessor + VectorStore + RAGLearner + Extractor")
        return parser, extractor

    except Exception as e:
        logger.warning(f"⚠️  服务初始化失败（{e}），自动降级到 --offline 模式")
        return None, None


def extract_real(
    parser, extractor, text: str, pdf_path: Optional[Path], use_rag: bool, ctype: str
) -> dict:
    """真实调用 extractor"""
    if pdf_path is not None and pdf_path.exists() and parser is not None:
        try:
            pdf_res = parser.extract_text(pdf_path.read_bytes())
            source_text = pdf_res.text or text
            logger.debug(f"  📄 PDF 解析成功：type={pdf_res.pdf_type}, chars={len(source_text)}")
        except Exception as e:
            logger.warning(f"  ⚠️  PDF 解析失败，用合成文本兜底: {e}")
            source_text = text
    else:
        source_text = text

    # extractor.extract() 旧 API 兼容（返回 dict）
    try:
        return extractor.extract(source_text, contract_type=ctype, use_few_shot=use_rag)
    except Exception as e1:
        logger.warning(f"  ⚠️  extract() 失败，尝试新 API extract_contract_fields(): {e1}")
        try:
            result = extractor.extract_contract_fields(
                source_text, contract_type=ctype, use_rag=use_rag, top_k_examples=3
            )
            return result.fields
        except Exception as e2:
            logger.error(f"  ❌ 新旧 API 都失败: {e2}")
            return {}


def extract_mock(annot: dict, use_rag: bool) -> dict:
    """
    offline 模式的 mock 预测：
    - use_rag=True 时完全返回 gold 值（模拟完美模型）
    - use_rag=False 时随机丢失约 20% 字段 + 部分值改写
      （用来演示 RAG ON/OFF 差异，不代表真实模型准确率）
    """
    import random
    rng = random.Random(hash(annot.get("id", "")) % 10000 + (1 if use_rag else 0))

    # mock 提取结果基 = 复制 annotation 的金标准字段
    pred = {}
    for k in EVAL_FIELDS:
        if k in annot and annot[k] is not None:
            pred[k] = annot[k]

    if use_rag:
        # RAG ON → 90%+ 准确（偶尔丢几个长文本字段）
        fields_to_drop = {"payment_terms", "breach_clause"}  # 长文本容易遗漏
        for f in list(pred.keys()):
            if f in fields_to_drop and rng.random() < 0.1:
                pred.pop(f)
    else:
        # RAG OFF → 65-70% 准确（丢更多字段 + 类型字段错判）
        fields_to_drop = {"payment_terms", "breach_clause", "amount_uppercase", "contract_code"}
        for f in list(pred.keys()):
            if f in fields_to_drop and rng.random() < 0.35:
                pred.pop(f)
        # 类型字段偶尔错判
        if rng.random() < 0.2 and "contract_type" not in annot:
            pass  # annotation 没有 contract_type 字段

    # 确保 contract_type 在 pred 里（extract 总返回这个）
    if "contract_type" not in pred:
        pred["contract_type"] = annot.get("contract_type", "其他")

    return pred


def run_single_evaluation(
    annotations: list[dict],
    pdf_dir: Optional[Path],
    parser,
    extractor,
    use_rag: bool,
    offline: bool,
) -> dict:
    """跑一组（RAG ON 或 RAG OFF）完整评估"""
    group_label = "RAG=ON (few-shot)" if use_rag else "RAG=OFF (base)"
    logger.info(f"\n{'='*60}")
    logger.info(f"🔄 评估组：{group_label}")
    logger.info(f"{'='*60}")

    per_contract = []
    field_correct = {f: 0 for f in EVAL_FIELDS}
    field_total = {f: 0 for f in EVAL_FIELDS}
    total_correct = 0
    total_fields = 0
    extracted_texts = []  # 收集用于 RAGAS 的合同文本

    for annot in annotations:
        cid = annot.get("id", "unknown")
        ctype = annot.get("contract_type", "其他")
        synthetic_text = synthesize_contract_text(annot)

        pdf_path = None
        if pdf_dir:
            pdf_path = pdf_dir / f"{cid}.pdf"
            if not pdf_path.exists():
                pdf_path = None

        if offline or extractor is None:
            pred = extract_mock(annot, use_rag)
            source_text = synthetic_text
        else:
            source_text = synthetic_text  # 真实 PDF 时会被覆盖
            pred = extract_real(parser, extractor, synthetic_text, pdf_path, use_rag, ctype)

        # 保存提取的文本（RAGAS 用）
        extracted_texts.append({
            "id": cid,
            "text": source_text,
            "pred": pred,
            "gold": annot,
        })

        # 对比
        field_results = compare_all_fields(pred, annot)
        correct = sum(1 for f in field_results.values() if f["match"])
        per_contract.append({
            "id": cid,
            "contract_type": ctype,
            "total_fields": len(EVAL_FIELDS),
            "correct_fields": correct,
            "accuracy": round(correct / len(EVAL_FIELDS), 4),
            "fields": field_results,
        })

        total_correct += correct
        total_fields += len(EVAL_FIELDS)
        for f in EVAL_FIELDS:
            field_total[f] += 1
            if field_results[f]["match"]:
                field_correct[f] += 1

        icon = "✅" if correct == len(EVAL_FIELDS) else "⚠️"
        logger.info(f"  {icon} {cid} | {correct}/{len(EVAL_FIELDS)} "
                     f"({correct / len(EVAL_FIELDS):.0%}) | type={ctype}")

    overall_acc = total_correct / total_fields if total_fields else 0
    field_acc = {
        f: round(field_correct[f] / field_total[f], 4) if field_total[f] else 0.0
        for f in EVAL_FIELDS
    }

    return {
        "group": group_label,
        "use_rag": use_rag,
        "mode": "offline_mock" if offline else "real",
        "overall_accuracy": round(overall_acc, 4),
        "total_fields": total_fields,
        "correct_fields": total_correct,
        "per_field_accuracy": field_acc,
        "per_contract": per_contract,
        "extracted_texts": extracted_texts,
    }


# ═══════════════════════════════════════════════════════════════════
# 5. RAG 指标计算（优先 RAGAS，降级自算）
# ═══════════════════════════════════════════════════════════════════
def _build_rag_pairs(round_result: dict) -> list[dict]:
    """
    从评估结果构造 RAG 问答对。
    每个字段 → {question, ground_truth, answer, context}
    - question：FIELD_QUESTIONS[field]
    - ground_truth：gold 值
    - answer：pred 值（extractor 抽出来的）
    - context：合同原文（extracted_text）
    """
    pairs = []
    for entry in round_result["extracted_texts"]:
        cid = entry["id"]
        gold = entry["gold"]
        pred = entry["pred"]
        text = entry["text"]

        for field in EVAL_FIELDS:
            g = gold.get(field)
            p = pred.get(field)
            # 跳过全 null 的问答对（没有 ground truth 就不评）
            if g is None and p is None:
                continue
            pairs.append({
                "id": f"{cid}.{field}",
                "question": FIELD_QUESTIONS.get(field, f"合同的 {FIELD_ZH.get(field, field)} 是什么？"),
                "ground_truth": str(g) if g is not None else "",
                "answer": str(p) if p is not None else "",
                "context": text,
                "field": field,
                "contract_id": cid,
            })

    logger.info(f"  📝 构造了 {len(pairs)} 个 RAG 问答对")
    return pairs


def _ragas_available() -> bool:
    try:
        import ragas  # noqa: F401
        return True
    except ImportError:
        return False


def _compute_simple_rag_metrics(pairs: list[dict]) -> dict:
    """
    自算版 RAG 指标（不依赖 ragas 库），4 个核心指标全覆盖：
    - faithfulness：answer 中的实体是否在 context 中找到支撑
    - answer_relevancy：answer 是否回答了 question 所询问的实体
    - context_precision：context 中被 answer 实际用到的信息占比
    - context_recall：ground_truth 的关键信息是否都被 context 覆盖
    """
    if not pairs:
        return {
            "faithfulness": 0.0, "answer_relevancy": 0.0,
            "context_precision": 0.0, "context_recall": 0.0,
            "note": "无问答对",
        }

    faith_scores, relevancy_scores = [], []
    ctx_prec_scores, ctx_rec_scores = [], []

    for pair in pairs:
        context = pair["context"]
        answer = pair["answer"]
        gt = pair["ground_truth"]
        field = pair["field"]

        if not answer:
            faith_scores.append(0.0)
            relevancy_scores.append(0.0)
            ctx_prec_scores.append(0.0)
            ctx_rec_scores.append(0.0)
            continue

        faith_scores.append(_compute_faithfulness(answer, context, field))
        relevancy_scores.append(_compute_relevancy(answer, gt, field))

        # ── Context Precision：context 中"被 answer 引用的块"占比 ──
        ctx_prec_scores.append(_compute_context_precision(answer, context, field))

        # ── Context Recall：ground_truth 的关键信息是否都在 context 中 ──
        ctx_rec_scores.append(_compute_context_recall(gt, context, field))

    n = len(pairs)
    return {
        "faithfulness": round(sum(faith_scores) / n, 4),
        "answer_relevancy": round(sum(relevancy_scores) / n, 4),
        "context_precision": round(sum(ctx_prec_scores) / n, 4),
        "context_recall": round(sum(ctx_rec_scores) / n, 4),
        "pair_count": n,
        "note": "自算版（ragas 依赖缺失或不可用）",
    }


def _compute_context_precision(answer: str, context: str, field: str) -> float:
    """
    Context Precision = answer 中从 context 抽出的关键信息占 context 总有效信息量的比例。
    策略：把 context 切成 N 个 token，看 answer 中命中了多少 context token。
    命中越多 → context 中被"有效使用"的部分越多 → precision 越高。
    """
    ctx_norm = _normalize_str(context)
    ans_norm = _normalize_str(answer)

    # 特殊字段：日期/金额/枚举 → 精确匹配命中
    if field in ("sign_date", "effective_date", "expire_date"):
        ans_date = _normalize_date(answer)
        ctx_dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", ctx_norm))
        if ans_date and ans_date in ctx_dates:
            return 1.0
        return 0.0

    if field == "amount":
        ans_amt = _normalize_amount(answer)
        ctx_nums = [float(s) for s in re.findall(r"\d+\.?\d*", context) if _try_float(s)]
        if ans_amt is not None and any(abs(ans_amt - c) / max(c, 1) < 0.02 for c in ctx_nums):
            return 1.0
        return 0.0

    # 默认：token 级 precision
    ctx_tokens = set(t for t in re.split(r"[，。；：、\s,;:]+", ctx_norm) if t and len(t) >= 2)
    ans_tokens = set(t for t in re.split(r"[，。；：、\s,;:]+", ans_norm) if t and len(t) >= 2)
    if not ctx_tokens or not ans_tokens:
        return 1.0 if ans_norm in ctx_norm else 0.0
    # 被 answer 用到的 context token 数 / context 总 token 数
    used = ctx_tokens & ans_tokens
    return len(used) / len(ctx_tokens)


def _compute_context_recall(ground_truth: str, context: str, field: str) -> float:
    """
    Context Recall = ground_truth 中的关键信息是否都能在 context 中找到。
    等价于 RAGAS 的 context_recall：retrieved_context 是否覆盖了 ground_truth 的所有断言。
    """
    if not ground_truth:
        return 1.0  # 无金标准 → 不算 recall 扣分

    ctx_norm = _normalize_str(context)
    gt_norm = _normalize_str(ground_truth)

    # 特殊字段：日期/金额 → 精确归一化比较
    if field in ("sign_date", "effective_date", "expire_date"):
        gt_date = _normalize_date(ground_truth)
        ctx_dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", ctx_norm))
        return 1.0 if gt_date and gt_date in ctx_dates else 0.0

    if field == "amount":
        gt_amt = _normalize_amount(ground_truth)
        ctx_nums = [float(s) for s in re.findall(r"\d+\.?\d*", context) if _try_float(s)]
        if gt_amt is None:
            return 0.0
        return 1.0 if any(abs(gt_amt - c) / max(c, 1) < 0.02 for c in ctx_nums) else 0.0

    # 默认：ground_truth 的 token 是否都在 context 中
    gt_tokens = [t for t in re.split(r"[，。；：、\s,;:]+", gt_norm) if t and len(t) >= 2]
    if not gt_tokens:
        return 1.0
    hits = sum(1 for t in gt_tokens if t in ctx_norm)
    return hits / len(gt_tokens)


def _try_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _compute_faithfulness(answer: str, context: str, field: str) -> float:
    """
    忠实度 = answer 中能在 context 中找到支撑的 token 比例。
    策略：把 answer 按标点/空格切成 token，统计在 context 中命中的比例。
    金额/日期字段特殊处理（归一化后匹配）。
    """
    if not answer or not context:
        return 0.0

    # 归一化 context 便于搜索
    ctx_norm = _normalize_str(context)
    ans_norm = _normalize_str(answer)

    # 统一日期格式匹配
    if field in ("sign_date", "effective_date", "expire_date"):
        ans_date = _normalize_date(answer)
        ctx_dates = re.findall(r"\d{4}-\d{2}-\d{2}", ctx_norm)
        if ans_date and ans_date in ctx_dates:
            return 1.0
        return 0.5  # context 有日期但格式不同

    # 统一金额匹配
    if field == "amount":
        ans_amt = _normalize_amount(answer)
        # context 里找所有数字
        ctx_nums = re.findall(r"\d+\.?\d*", context)
        ctx_floats = []
        for s in ctx_nums:
            try:
                ctx_floats.append(float(s))
            except ValueError:
                pass
        if ans_amt is not None and any(abs(ans_amt - c) / max(c, 1) < 0.02 for c in ctx_floats):
            return 1.0
        return 0.3

    # 枚举字段：精确/包含匹配
    if field == "dispute_resolution":
        mapping = {"诉讼": "诉讼", "仲裁": "仲裁", "协商": "协商"}
        ans_key = _normalize_str(answer).replace("诉讼", "诉讼").replace("仲裁", "仲裁").replace("协商", "协商")
        for cn, kw in mapping.items():
            if kw in ans_key and kw in ctx_norm:
                return 1.0
        return 0.5

    # 默认：token 级别
    tokens = [t for t in re.split(r"[，。；：、\s,;:]+", ans_norm) if t]
    if not tokens:
        return 1.0 if ans_norm in ctx_norm else 0.0
    hits = sum(1 for t in tokens if t and t in ctx_norm)
    return hits / len(tokens)


def _compute_relevancy(answer: str, ground_truth: str, field: str) -> float:
    """
    答案相关性 = answer 与 ground_truth 的语义重叠度。
    用 field_match 的结果做近似：匹配 → 1.0，否则看包含度。
    """
    if not ground_truth and not answer:
        return 1.0
    if not ground_truth or not answer:
        return 0.0

    # 用已有的 field_match 逻辑
    if field_match(answer, ground_truth, field):
        return 1.0

    # 包含/子串关系
    na = _normalize_str(answer)
    ng = _normalize_str(ground_truth)
    if na and ng and (na in ng or ng in na):
        return 0.7

    # token 重叠
    tokens_a = set(re.split(r"[，。；：、\s,;:]+", na))
    tokens_g = set(re.split(r"[，。；：、\s,;:]+", ng))
    tokens_a.discard("")
    tokens_g.discard("")
    if tokens_a and tokens_g:
        overlap = tokens_a & tokens_g
        return len(overlap) / max(len(tokens_a), len(tokens_g))

    return 0.0


def compute_rag_metrics(round_result: dict, use_ragas: bool = True) -> dict:
    """计算 RAG 指标"""
    pairs = _build_rag_pairs(round_result)

    if not use_ragas or not _ragas_available():
        if use_ragas and not _ragas_available():
            logger.info("  📦 ragas 库未安装 → 使用自算版 faithfulness/relevancy")
        return _compute_simple_rag_metrics(pairs)

    # ── ragas 库路线（4 个核心指标，ragas 0.4.x collections API） ──
    try:
        import asyncio
        import os

        from openai import AsyncOpenAI
        from ragas.embeddings import HuggingFaceEmbeddings
        from ragas.llms import llm_factory
        from ragas.metrics.collections.answer_relevancy import AnswerRelevancy
        from ragas.metrics.collections.context_precision import ContextPrecision
        from ragas.metrics.collections.context_recall import ContextRecall
        from ragas.metrics.collections.faithfulness import Faithfulness

        from app.config import get_settings

        # LLM-as-judge 逐条调用很慢（240 对全跑约 2000+ 次 judge），默认抽样 30 条
        max_pairs = int(os.environ.get("RAGAS_MAX_PAIRS", "30"))
        sample = pairs if len(pairs) <= max_pairs else pairs[:: len(pairs) // max_pairs][:max_pairs]

        _s = get_settings()
        # ascore() 是异步 API，judge client 必须用 AsyncOpenAI（同步 client 会报
        # "Cannot use agenerate() with a synchronous client"）
        judge = llm_factory(
            _s.llm_model,
            client=AsyncOpenAI(api_key=_s.llm_api_key, base_url=_s.llm_base_url),
        )
        embedder = HuggingFaceEmbeddings(model=_s.embedding_model)

        f_metric = Faithfulness(llm=judge)
        ar_metric = AnswerRelevancy(llm=judge, embeddings=embedder)
        cp_metric = ContextPrecision(llm=judge)
        cr_metric = ContextRecall(llm=judge)

        async def _run_all():
            sums = {"faithfulness": 0.0, "answer_relevancy": 0.0,
                    "context_precision": 0.0, "context_recall": 0.0}
            counts = dict.fromkeys(sums, 0)

            async def _score(metric, key, **kw):
                try:
                    r = await metric.ascore(**kw)
                    sums[key] += float(r.value)
                    counts[key] += 1
                except Exception as exc:  # 单条失败不影响整体
                    logger.warning(f"    {key} 单条失败: {exc}")

            for p in sample:
                ctx = [p["context"][:2000]]
                await _score(f_metric, "faithfulness",
                             user_input=p["question"], response=p["answer"],
                             retrieved_contexts=ctx)
                await _score(ar_metric, "answer_relevancy",
                             user_input=p["question"], response=p["answer"])
                await _score(cp_metric, "context_precision",
                             user_input=p["question"], reference=p["ground_truth"],
                             retrieved_contexts=ctx)
                await _score(cr_metric, "context_recall",
                             user_input=p["question"],
                             retrieved_contexts=ctx, reference=p["ground_truth"])
            return sums, counts

        sums, counts = asyncio.run(_run_all())
        if min(counts.values()) == 0:
            raise RuntimeError("ragas 单条调用全部失败")

        return {
            k: round(sums[k] / max(counts[k], 1), 4)
            for k in ("faithfulness", "answer_relevancy",
                      "context_precision", "context_recall")
        } | {
            "pair_count": len(sample),
            "note": f"ragas 0.4.3 LLM-as-judge（DeepSeek，抽样 {len(sample)}/{len(pairs)} 条）",
        }
    except Exception as e:
        logger.warning(f"  ⚠️  ragas 调用失败（{e}），回退到自算版")
        return _compute_simple_rag_metrics(pairs)


# ═══════════════════════════════════════════════════════════════════
# 6. Markdown 报告生成
# ═══════════════════════════════════════════════════════════════════
def generate_markdown_report(
    round_off: dict,
    round_on: dict,
    ragas_off: dict,
    ragas_on: dict,
    timestamp: str,
    annot_count: int,
    pdf_available: bool,
) -> str:
    """生成完整 Markdown 评估报告"""
    lines = []
    w = lines.append

    w(f"# M15 合同字段提取评估报告")
    w("")
    w(f"> 生成时间：{timestamp}")
    w(f"> 标注数量：{annot_count} 份金标准")
    w(f"> PDF 可用：{'是' if pdf_available else '否（使用合成合同文本）'}")
    w(f"> 评估模式：{round_on['mode']}")
    w("")

    # ── 总览 ──
    w("## 一、总体准确率对比")
    w("")
    w("| 方案 | 总字段数 | 正确数 | 准确率 |")
    w("|------|----------|--------|--------|")
    w(f"| RAG=OFF（基线） | {round_off['total_fields']} | {round_off['correct_fields']} | **{round_off['overall_accuracy']:.2%}** |")
    w(f"| RAG=ON（few-shot） | {round_on['total_fields']} | {round_on['correct_fields']} | **{round_on['overall_accuracy']:.2%}** |")
    delta = (round_on["overall_accuracy"] - round_off["overall_accuracy"]) * 100
    sign = "+" if delta >= 0 else ""
    w(f"| **RAG 提升** | — | — | **{sign}{delta:.1f}pp** |")
    w("")

    # ── 逐字段准确率 ──
    w("## 二、逐字段准确率")
    w("")
    w("| 字段 | 中文名 | RAG=OFF | RAG=ON | 差异 |")
    w("|------|--------|---------|--------|------|")
    for f in EVAL_FIELDS:
        zh = FIELD_ZH.get(f, f)
        off_acc = round_off["per_field_accuracy"][f]
        on_acc = round_on["per_field_accuracy"][f]
        d = (on_acc - off_acc) * 100
        ds = f"+{d:.1f}pp" if d >= 0 else f"{d:.1f}pp"
        w(f"| `{f}` | {zh} | {off_acc:.0%} | {on_acc:.0%} | {ds} |")
    w("")

    # ── RAG 指标（4 个核心） ──
    w("## 三、RAG 指标")
    w("")
    w("| 指标 | RAG=OFF | RAG=ON | 差异 | 解读 |")
    w("|------|---------|--------|------|------|")
    rag_metrics = [
        ("faithfulness",      "忠实度",   "answer 是否全部源于 context（无幻觉）"),
        ("answer_relevancy",  "相关性",   "answer 是否直接回答了 question"),
        ("context_precision", "精度",     "context 中被 answer 有效利用的信息占比"),
        ("context_recall",    "召回率",   "ground_truth 的关键信息是否都被 context 覆盖"),
    ]
    for metric, zh, desc in rag_metrics:
        off_v = ragas_off.get(metric, 0.0)
        on_v = ragas_on.get(metric, 0.0)
        d = (on_v - off_v) * 100
        ds = f"+{d:.1f}pp" if d >= 0 else f"{d:.1f}pp"
        w(f"| {metric} | {off_v:.3f} | {on_v:.3f} | {ds} | {desc} |")
    w("")
    w(f"问答对数量：{ragas_on.get('pair_count', 0)}  |  计算方式：{ragas_on.get('note', '—')}")
    w("")

    # ── 逐合同详情（RAG=ON） ──
    w("## 四、逐合同详情（RAG=ON）")
    w("")
    for cr in round_on["per_contract"]:
        icon = "✅" if cr["accuracy"] == 1.0 else "⚠️"
        w(f"### {icon} {cr['id']}（{cr['contract_type']}）— {cr['accuracy']:.0%}")
        w("")
        w("| 字段 | 预测值 | 金标准 | 匹配 |")
        w("|------|--------|--------|------|")
        for f in EVAL_FIELDS:
            fr = cr["fields"][f]
            pred_str = fr["pred"] or "—"
            gold_str = fr["gold"] or "—"
            mark = "✅" if fr["match"] else "❌"
            w(f"| {FIELD_ZH.get(f, f)} | `{pred_str}` | `{gold_str}` | {mark} |")
        w("")

    # ── 逐合同详情（RAG=OFF）只列 RAG=ON 失败的 ──
    w("## 五、RAG=OFF 遗漏详情（用于对比）")
    w("")
    for cr_on, cr_off in zip(round_on["per_contract"], round_off["per_contract"]):
        missed_off = [(f, cr_off["fields"][f]) for f in EVAL_FIELDS if not cr_off["fields"][f]["match"]]
        missed_on = [(f, cr_on["fields"][f]) for f in EVAL_FIELDS if not cr_on["fields"][f]["match"]]
        only_on_fixed = [m for m in missed_off if m[0] not in [x[0] for x in missed_on]]

        if only_on_fixed:
            w(f"**{cr_on['id']}** — RAG 修复了 {len(only_on_fixed)} 个字段：")
            for f, fr in only_on_fixed:
                w(f"  - ✅ {FIELD_ZH.get(f, f)}：`{fr['gold']}` ← RAG 抽取成功（OFF 时缺失/错误）")
            w("")

    # ── 分析结论 ──
    w("## 六、分析结论")
    w("")
    if delta >= 5:
        w(f"✅ **RAG 效果显著**：准确率从 {round_off['overall_accuracy']:.0%} 提升到 {round_on['overall_accuracy']:.0%}，提升 {sign}{delta:.1f}pp。")
    elif delta >= 0:
        w(f"⚠️ **RAG 有正向效果但不显著**：准确率 {sign}{delta:.1f}pp，考虑增加 few-shot 范例数量。")
    else:
        w(f"❌ **RAG 反而降低了准确率**：{sign}{delta:.1f}pp，请检查范例质量和检索逻辑。")
    w("")

    # RAG 指标解读（4 个）
    for metric, zh, desc in [
        ("faithfulness",      "Faithfulness",      "是否减少 LLM 幻觉（从范例原文摘录而非编造）"),
        ("answer_relevancy",  "Answer Relevancy",  "答案是否更直接回应了问题（而非答非所问）"),
        ("context_precision", "Context Precision", "检索到的 context 是否更精准地命中了答案所需信息"),
        ("context_recall",    "Context Recall",    "检索到的 context 是否完整覆盖了 ground_truth 的信息"),
    ]:
        diff = (ragas_on.get(metric, 0) - ragas_off.get(metric, 0)) * 100
        w(f"- **{zh}** 提升 {diff:+.1f}pp → {desc}")
    w("")
    w("> ⚠️ 注：如果是 offline 模式，RAG 指标是用 mock 预测值算的，"
      "仅展示评估框架的可用性，不代表真实模型表现。")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 7. 入口
# ═══════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="M15 · 合同字段提取评估与报告")
    parser.add_argument("--annot-dir", type=Path, default=BASE_DIR / "examples" / "annotations",
                        help="金标准标注目录")
    parser.add_argument("--pdf-dir", type=Path, default=BASE_DIR / "examples" / "gold_contracts",
                        help="PDF 合同目录（可选，没有就用合成文本）")
    parser.add_argument("--offline", action="store_true",
                        help="离线模式：不调 LLM，用 annotation 直接拿 mock 预测值")
    parser.add_argument("--no-ragas", action="store_true",
                        help="跳过 RAGAS 计算（强制用自算版）")
    parser.add_argument("--output-dir", type=Path, default=BASE_DIR / "reports",
                        help="报告输出目录")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Step 1: 加载标注 ──
    annotations = load_annotations(args.annot_dir)
    if not annotations:
        logger.error("❌ 没有找到任何标注，退出")
        sys.exit(1)

    # ── Step 2: 检查 PDF 目录 ──
    pdf_available = args.pdf_dir.exists() and any(args.pdf_dir.glob("*.pdf"))
    if pdf_available:
        logger.info(f"📄 找到真实 PDF 合同在 {args.pdf_dir}")
    else:
        logger.info("📝 没有真实 PDF，将使用合成合同文本")

    # ── Step 3: 初始化服务（或 mock） ──
    t0 = time.time()
    parser_obj, extractor = load_services_or_mock(offline=args.offline)
    actual_offline = args.offline or extractor is None
    logger.info(f"⏱️  服务初始化耗时 {time.time() - t0:.1f}s | offline={actual_offline}")

    # ── Step 4: 跑两组评估 ──
    t_eval = time.time()
    round_off = run_single_evaluation(
        annotations, args.pdf_dir if pdf_available else None,
        parser_obj, extractor, use_rag=False, offline=actual_offline,
    )
    round_on = run_single_evaluation(
        annotations, args.pdf_dir if pdf_available else None,
        parser_obj, extractor, use_rag=True, offline=actual_offline,
    )
    logger.info(f"⏱️  两组评估总耗时 {time.time() - t_eval:.1f}s")

    # ── Step 5: 计算 RAG 指标 ──
    logger.info("\n📊 计算 RAG 指标...")
    ragas_off = compute_rag_metrics(round_off, use_ragas=not args.no_ragas)
    ragas_on = compute_rag_metrics(round_on, use_ragas=not args.no_ragas)

    # ── Step 6: 生成报告 ──
    args.output_dir.mkdir(parents=True, exist_ok=True)

    md_report = generate_markdown_report(
        round_off, round_on, ragas_off, ragas_on,
        timestamp, len(annotations), pdf_available,
    )
    md_path = args.output_dir / f"m15_evaluation_{timestamp}.md"
    md_path.write_text(md_report, encoding="utf-8")
    logger.info(f"📝 Markdown 报告: {md_path}")

    # JSON 原始数据
    raw = {
        "timestamp": timestamp,
        "annot_count": len(annotations),
        "pdf_available": pdf_available,
        "mode": round_on["mode"],
        "ragas_used": _ragas_available() and not args.no_ragas,
        "round_off": round_off,
        "round_on": round_on,
        "ragas_off": ragas_off,
        "ragas_on": ragas_on,
    }
    json_path = args.output_dir / f"m15_evaluation_{timestamp}.json"
    json_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"💾 原始数据: {json_path}")

    # ── 控制台速览 ──
    delta = (round_on["overall_accuracy"] - round_off["overall_accuracy"]) * 100
    sign = "+" if delta >= 0 else ""
    print(f"\n{'='*60}")
    print(f"📊 M15 评估完成")
    print(f"{'='*60}")
    print(f"  字段准确率  OFF→ON: {round_off['overall_accuracy']:.2%} → {round_on['overall_accuracy']:.2%}  ({sign}{delta:.1f}pp)")
    for m in ("faithfulness", "answer_relevancy", "context_precision", "context_recall"):
        off_v = ragas_off.get(m, "N/A")
        on_v = ragas_on.get(m, "N/A")
        if isinstance(off_v, float) and isinstance(on_v, float):
            d = (on_v - off_v) * 100
            ds = f"+{d:.1f}pp" if d >= 0 else f"{d:.1f}pp"
            print(f"  {m:20s} OFF={off_v:.3f}  ON={on_v:.3f}  ({ds})")
        else:
            print(f"  {m:20s} OFF={off_v}  ON={on_v}")
    print(f"  {'='*60}")
    print(f"  Markdown 报告: {md_path}")
    print(f"  JSON 数据:     {json_path}")


if __name__ == "__main__":
    main()

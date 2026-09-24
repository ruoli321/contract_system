"""
准确率测试脚本 —— 金标准回归测试
对比：无 few-shot vs 有 few-shot 的字段提取准确率（leave-one-out 防数据泄漏）

用法（在容器内执行）：
    # 同时跑两组对比（最常用）
    docker exec contract-ai python /app/scripts/evaluate_accuracy.py

    # 只跑 few-shot 开启
    docker exec contract-ai python /app/scripts/evaluate_accuracy.py --fewshot

    # 只跑 few-shot 关闭
    docker exec contract-ai python /app/scripts/evaluate_accuracy.py --no-fewshot
"""
import argparse
import json
import logging
import sys
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("eval")

BASE_DIR = Path(__file__).resolve().parent.parent

# 提取准确率的字段（与 extractor.py 的 Schema 一致）
EVAL_FIELDS = [
    "contract_name",
    "contract_code",
    "partner_a",
    "partner_b",
    "amount",
    "sign_date",
    "effective_date",
    "expire_date",
    "payment_terms",
    "breach_clause",
    "dispute_resolution",
]


    return np == ng or np in ng or ng in np


def load_services():
    """初始化所有需要的服务（新架构：Extractor 依赖 RAGLearner）"""
    sys.path.insert(0, str(BASE_DIR))

    from app.config import get_settings
    from app.services.vector_store import VectorStore
    from app.services.rag_learner import RAGLearner
    from app.services.extractor import ContractExtractor
    from app.services.prompt_manager import PromptManager
    from app.services.llm_client import create_llm

    settings = get_settings()

    # LLM
    llm = create_llm(
        provider=settings.llm_provider,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
    )

    # VectorStore + RAGLearner
    vs = VectorStore(
        host=settings.chroma_host,
        port=settings.chroma_port,
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
    )
    rag = RAGLearner(vector_store=vs, collection_name=settings.chroma_collection_examples)

    # PromptManager
    pm = PromptManager(prompts_dir=settings.prompts_dir)

    # 加载范例（从 annotations + _合同原文 + test_pdfs 自动发现）
    annot_dir = BASE_DIR / "examples" / "annotations"
    texts_dir = BASE_DIR.parent / "test_pdfs"
    rag.load_examples(
        annotations_dir=str(annot_dir),
        texts_dir=str(texts_dir) if texts_dir.exists() else None,
        reset=False,
    )
    stats = rag.stats()
    logger.info(f"  📚 RAGLearner 已加载 {stats['total_examples']} 份范例 | {stats['total_chunks_in_chroma']} chunks")

    # Extractor（新签名：llm_client, prompt_manager, rag_learner）
    extractor = ContractExtractor(
        llm_client=llm,
        prompt_manager=pm,
        rag_learner=rag,
    )

    return extractor


def discover_eval_cases(only_ids: list = None) -> list[tuple[str, dict]]:
    """
    自动发现可用于评估的 (contract_text, gold_annotation) 对。
    查找链：annotation.text → annotation._合同原文 → test_pdfs/{id}.txt
    
    Args:
        only_ids: 若传入则只评估这些 ID 的 annotation，否则评估全部
    """
    annot_dir = BASE_DIR / "examples" / "annotations"
    texts_dir = BASE_DIR.parent / "test_pdfs"

    if not annot_dir.exists():
        logger.error(f"标注目录不存在: {annot_dir}")
        return []

    cases = []
    for jf in sorted(annot_dir.glob("*.json")):
        if jf.name.startswith("_"):
            continue
        try:
            annot = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue

        ex_id = annot.get("id")
        if not ex_id:
            continue
        if only_ids and ex_id not in only_ids:
            continue

        # 合同原文查找链（与 RAGLearner.load_examples 一致）
        text = annot.get("text") or annot.get("_合同原文")
        if not text or len(text.strip()) < 50:
            txt_file = texts_dir / f"{ex_id}.txt"
            if txt_file.exists():
                text = txt_file.read_text(encoding="utf-8")

        if not text or len(text.strip()) < 50:
            logger.warning(f"  ⚠️  {ex_id}: 缺少合同原文，跳过评估")
            continue

        cases.append((text, annot))

    return cases


def run_eval(use_few_shot: bool, only_ids: list = None) -> dict:
    """跑一轮完整评估（新架构 + leave-one-out）"""
    extractor = load_services()

    cases = discover_eval_cases(only_ids=only_ids)
    if not cases:
        logger.error("没有发现可用于评估的合同样本")
        return {}

    logger.info(f"📋 发现 {len(cases)} 份可评估合同")
    total_fields, correct_fields = 0, 0
    per_contract_results = []

    for text, gold in cases:
        ex_id = gold["id"]
        ctype = gold.get("contract_type", "其他")

        # leave-one-out：排除当前评估样本自身，防止数据泄漏虚高准确率
        _exclude = [ex_id] if use_few_shot else None

        result = extractor.extract_contract_fields(
            contract_text=text,
            contract_type=ctype,
            use_rag=use_few_shot,
            top_k_examples=3,
            exclude_ids=_exclude,
        )
        extraction = result.fields

        fs_info = f"[{result.few_shot_count} 范例 {' '.join(result.few_shot_sources) if result.few_shot_sources else ''}]"
        contract_result = {"id": ex_id, "contract_type": ctype, "few_shot_info": fs_info, "fields": {}}

        for field in EVAL_FIELDS:
            pred_val = extraction.get(field)
            gold_val = gold.get(field)
            matched = field_match(pred_val, gold_val)
            contract_result["fields"][field] = {
                "pred": str(pred_val)[:80] if pred_val else None,
                "gold": str(gold_val)[:80] if gold_val else None,
                "match": matched,
            }
            total_fields += 1
            if matched:
                correct_fields += 1

        per_contract_results.append(contract_result)
        correct_cnt = sum(1 for f in contract_result["fields"].values() if f["match"])
        icon = "✅" if correct_cnt == len(EVAL_FIELDS) else "⚠️"
        logger.info(f"  {icon} {ex_id} | {correct_cnt}/{len(EVAL_FIELDS)} {fs_info}")

    accuracy = correct_fields / total_fields if total_fields else 0

    return {
        "few_shot": use_few_shot,
        "total_fields": total_fields,
        "correct_fields": correct_fields,
        "accuracy": round(accuracy, 4),
        "per_contract": per_contract_results,
        "timestamp": datetime.now().isoformat(),
    }


def print_report(result: dict):
    """打印评估报告"""
    if not result:
        return

    fs_label = "ON (有 few-shot)" if result["few_shot"] else "OFF (无 few-shot)"
    print(f"\n{'='*60}")
    print(f"📊 准确率测试报告 | Few-shot: {fs_label}")
    print(f"{'='*60}")
    print(f"  总字段数: {result['total_fields']}")
    print(f"  正确字段: {result['correct_fields']}")
    print(f"  准确率:   {result['accuracy']:.2%}")
    print(f"{'='*60}")

    for cr in result["per_contract"]:
        correct = sum(1 for f in cr["fields"].values() if f["match"])
        print(f"\n  📄 {cr['id']} ({correct}/{len(EVAL_FIELDS)}) {cr.get('few_shot_info', '')}")
        for field, info in cr["fields"].items():
            icon = "✅" if info["match"] else "❌"
            if not info["match"]:
                print(f"    {icon} {field}:")
                print(f"       预期: {info['gold']}")
                print(f"       实际: {info['pred']}")


def save_report(result: dict):
    """保存报告"""
    report_dir = BASE_DIR / "reports"
    report_dir.mkdir(exist_ok=True)

    json_path = report_dir / f"accuracy_fewshot_{int(result['few_shot'])}_{datetime.now():%Y%m%d_%H%M%S}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 完整报告已保存: {json_path}")


def main():
    parser = argparse.ArgumentParser(description="金标准准确率回归测试（leave-one-out）")
    parser.add_argument("--fewshot", action="store_true", help="只跑 few-shot 开启")
    parser.add_argument("--no-fewshot", action="store_true", help="只跑 few-shot 关闭")
    parser.add_argument("--only-ids", type=str, default=None,
                        help="只评估指定 IDs（逗号分隔），默认评估全部")
    args = parser.parse_args()

    only_ids = [x.strip() for x in args.only_ids.split(",")] if args.only_ids else None

    results = []

    if args.fewshot:
        print("\n🔄 评估模式：Few-shot ON")
        r = run_eval(use_few_shot=True, only_ids=only_ids)
        results.append(r)
        print_report(r)
        save_report(r)
    elif args.no_fewshot:
        print("\n🔄 评估模式：Few-shot OFF")
        r = run_eval(use_few_shot=False, only_ids=only_ids)
        results.append(r)
        print_report(r)
        save_report(r)
    else:
        # 默认：先跑 OFF，再跑 ON，最后对比
        print("\n" + "🎯" * 20)
        print("🎯 金标准准确率对比实验（Few-shot OFF → ON，leave-one-out）")
        print("🎯" * 20)

        if only_ids:
            print(f"\n📋 评估子集: {len(only_ids)} 份合同（仅核心评估集）")

        print("\n\n🔄 第 1 轮：Few-shot OFF（基线）")
        r_off = run_eval(use_few_shot=False, only_ids=only_ids)
        print_report(r_off)
        save_report(r_off)

        print("\n\n🔄 第 2 轮：Few-shot ON（RAG 增强）")
        r_on = run_eval(use_few_shot=True, only_ids=only_ids)
        print_report(r_on)
        save_report(r_on)

        # 对比总结
        print(f"\n\n{'='*60}")
        print(f"📈 对比总结")
        print(f"{'='*60}")
        print(f"  Few-shot OFF: {r_off['accuracy']:.2%}")
        print(f"  Few-shot ON:  {r_on['accuracy']:.2%}")
        diff = (r_on["accuracy"] - r_off["accuracy"]) * 100
        sign = "+" if diff >= 0 else ""
        print(f"  提升幅度:     {sign}{diff:.1f}pp")

        if diff >= 5:
            print(f"  ✅ RAG few-shot 效果显著（提升 {sign}{diff:.1f}pp）")
        elif diff >= 0:
            print(f"  ⚠️  RAG few-shot 有正向效果但不够明显（提升 {sign}{diff:.1f}pp），考虑增加范例数量")
        else:
            print(f"  ❌ RAG few-shot 反而降低了准确率！请检查范例质量和检索逻辑")

        results = [r_off, r_on]

        # 保存汇总对比
        summary = {
            "timestamp": datetime.now().isoformat(),
            "comparison": {
                "fewshot_off_accuracy": r_off["accuracy"],
                "fewshot_on_accuracy": r_on["accuracy"],
                "improvement_pp": round(diff, 2),
            },
            "detail": results,
        }
        summary_path = BASE_DIR / "reports" / f"accuracy_comparison_{datetime.now():%Y%m%d_%H%M%S}.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n💾 对比报告已保存: {summary_path}")

        # 答辩用的表格格式
        print(f"\n\n📋 答辩数据（复制到 PPT）")
        print(f"{'='*40}")
        print(f"| 方案 | 准确率 | 提升 |")
        print(f"|------|--------|------|")
        print(f"| 无 Few-shot | {r_off['accuracy']:.2%} | — |")
        print(f"| 有 Few-shot | {r_on['accuracy']:.2%} | {sign}{diff:.1f}pp |")
        print(f"{'='*40}")


if __name__ == "__main__":
    main()

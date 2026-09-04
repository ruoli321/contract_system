"""
金标准合同范例入库脚本（统一入口）

扫描 examples/annotations/*.json 标注文件
→ 自动发现合同原文（annotation.text → annotation._合同原文 → test_pdfs/{id}.txt）
→ 切块入库 Chroma contract_examples + 填充 RAGLearner 内存缓存

用法：
    # 在 Docker 容器内执行（推荐，Chroma 在容器网络里）
    docker exec contract-ai python /app/scripts/bootstrap_examples.py

    # 本地直接执行（需要 Chroma 在 localhost:8001 运行）
    cd ai-service && python scripts/bootstrap_examples.py

    # 清空 Chroma 旧数据 + 内存缓存，重新全量入库
    python scripts/bootstrap_examples.py --reset

    # 指定 test_pdfs 目录（默认自动从项目根目录向上找）
    python scripts/bootstrap_examples.py --texts-dir ../test_pdfs
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # ai-service/


def init_learner(texts_dir_override: str = None):
    """初始化 VectorStore + RAGLearner（延迟加载 Embedding 模型）"""
    sys.path.insert(0, str(BASE_DIR))

    from app.services.vector_store import VectorStore
    from app.services.rag_learner import RAGLearner
    from app.config import get_settings

    settings = get_settings()

    # texts_dir: 优先用命令行参数 → 其次 settings → 最后自动探测
    texts_dir = texts_dir_override or settings.test_pdfs_dir
    if not texts_dir:
        auto_test_pdfs = BASE_DIR.parent / "test_pdfs"
        if auto_test_pdfs.exists():
            texts_dir = str(auto_test_pdfs)

    vs = VectorStore(
        host=settings.chroma_host,
        port=settings.chroma_port,
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
    )
    learner = RAGLearner(
        vector_store=vs,
        collection_name=settings.chroma_collection_examples,
    )
    return learner, texts_dir


def bootstrap(reset: bool = False, texts_dir_override: str = None):
    ANNOT_DIR = BASE_DIR / "examples" / "annotations"

    if not ANNOT_DIR.exists():
        print(f"❌ 标注目录不存在: {ANNOT_DIR}")
        sys.exit(1)

    learner, texts_dir = init_learner(texts_dir_override)

    print(f"\n{'='*50}")
    print(f"📚 金标准合同范例入库")
    print(f"{'='*50}")
    print(f"  标注目录: {ANNOT_DIR}")
    print(f"  原文目录: {texts_dir or '(annotation内嵌或跳过)'}")
    print(f"  Chroma 集合: {learner.collection_name}")
    print(f"  重置模式: {'是' if reset else '否（upsert）'}")

    # 调用增强后的 load_examples — 自动从 annotation.text / _合同原文 / texts_dir/{id}.txt 取文本
    results = learner.load_examples(
        annotations_dir=str(ANNOT_DIR),
        texts_dir=texts_dir,
        reset=reset,
    )

    success = sum(1 for r in results if "error" not in r)
    failed = sum(1 for r in results if "error" in r)
    stats = learner.stats()

    print(f"\n{'='*50}")
    print(f"📊 入库完成")
    print(f"  成功入库: {success} 份")
    if failed:
        print(f"  失败: {failed} 份")
    print(f"  内存缓存: {stats['total_examples']} 份")
    print(f"  Chroma 总块数: {stats['total_chunks_in_chroma']}")
    print(f"  按合同类型分布:")
    for ctype, count in sorted(stats.get("by_contract_type", {}).items()):
        print(f"      {ctype}: {count} 份")
    print(f"{'='*50}")

    if success < 8:
        print(f"\n⚠️  提示：考核要求 ≥4 类合同各 ≥2 份范例（共 ≥8 份）")
        print(f"   当前成功 {success} 份。跳过的 annotation 缺少合同原文，")
        print(f"   可补充 annotation['text'] 字段或对应的 test_pdfs/{id}.txt")
    else:
        print(f"\n✅ 达标！{success} 份范例 ≥ 考核要求 8 份")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="合同范例入库脚本")
    ap.add_argument("--reset", action="store_true", help="重置模式：先清空 Chroma + 内存缓存再全量入库")
    ap.add_argument("--texts-dir", type=str, default=None, help="合同原文 txt 目录（默认自动探测项目根 test_pdfs/）")
    args = ap.parse_args()
    bootstrap(reset=args.reset, texts_dir_override=args.texts_dir)

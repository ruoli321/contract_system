"""
标注模板批量生成脚本

用法：
    # 为 gold_contracts/ 下所有 PDF 生成空白标注文件
    python generate_annotation.py

    # 为指定类型生成
    python generate_annotation.py --type purchase
"""
import argparse
import json
import sys
from pathlib import Path


ANNOTATIONS_DIR = Path(__file__).parent.parent / "examples" / "annotations"
GOLD_DIR = Path(__file__).parent.parent / "examples" / "gold_contracts"

TYPE_MAP = {
    "purchase": "采购合同",
    "sales": "销售合同",
    "service": "服务合同",
    "lease": "租赁合同",
    "other": "其他",
}


TEMPLATE = {
    "id": "",
    "contract_type": "",
    "contract_name": "",
    "contract_code": None,
    "partner_a": "",
    "partner_b": "",
    "amount": None,
    "amount_uppercase": None,
    "sign_date": None,
    "effective_date": None,
    "expire_date": None,
    "payment_terms": None,
    "breach_clause": None,
    "dispute_resolution": None,
}


def infer_contract_type(filename: str) -> str:
    """从文件名推断合同类型"""
    lower = filename.lower()
    for key, value in TYPE_MAP.items():
        if key in lower:
            return value
    return "其他"


def generate_for_pdf(pdf_path: Path, force: bool = False) -> bool:
    """为单个 PDF 生成标注文件"""
    annot_path = ANNOTATIONS_DIR / f"{pdf_path.stem}.json"
    
    if annot_path.exists() and not force:
        print(f"  ⏭️  已存在，跳过: {annot_path.name}")
        return False
    
    ctype = infer_contract_type(pdf_path.name)
    
    template = TEMPLATE.copy()
    template["id"] = pdf_path.stem
    template["contract_type"] = ctype
    template["contract_name"] = f"（从 PDF 标题提取）"
    
    ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    annot_path.write_text(
        json.dumps(template, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  ✅ 已生成: {annot_path.name} → 类型推断为「{ctype}」")
    return True


def main():
    parser = argparse.ArgumentParser(description="批量生成金标准合同标注模板")
    parser.add_argument("--type", "-t", choices=list(TYPE_MAP.keys()),
                        help="只处理指定类型的 PDF")
    parser.add_argument("--force", "-f", action="store_true",
                        help="覆盖已存在的标注文件")
    args = parser.parse_args()
    
    if not GOLD_DIR.exists():
        print(f"❌ 金标准合同目录不存在: {GOLD_DIR}")
        print("   请先将 PDF 放入 examples/gold_contracts/ 目录")
        sys.exit(1)
    
    pdf_files = sorted(GOLD_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"⚠️  {GOLD_DIR} 下没有 PDF 文件")
        sys.exit(0)
    
    print(f"\n📂 扫描到 {len(pdf_files)} 份 PDF\n")
    
    created = 0
    for pdf in pdf_files:
        if args.type and args.type not in pdf.name.lower():
            continue
        if generate_for_pdf(pdf, force=args.force):
            created += 1
    
    print(f"\n✅ 完成：新建 {created} 份标注模板，共 {len(pdf_files)} 份 PDF")
    print(f"📖 标注指南: 打开 annotations/_TEMPLATE.json 查看每个字段怎么填")
    print(f"📝 标注完后运行: python scripts/bootstrap_examples.py 入库")


if __name__ == "__main__":
    main()

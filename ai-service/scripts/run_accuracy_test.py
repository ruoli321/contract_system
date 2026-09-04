"""M18 · 简化版准确率测试脚本

读取指定目录下的合同 PDF + 标注 JSON → 调用 AI 服务 API → 输出准确率报告。

与 evaluate_m15.py 的区别：
    - 更轻量：不需要 LLM RAGAS 评估
    - 直接走 HTTP 调用（不需要 import 内部类）
    - 专注于「字段准确率」单一指标

用法:
    python run_accuracy_test.py --annot-dir examples/annotations --pdf-dir /path/to/pdfs
    python run_accuracy_test.py --annot-dir examples/annotations --offline    # 离线 mock 演示
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── HTTP 客户端（延迟导入，避免无依赖时崩） ──
try:
    import requests
except ImportError:
    requests = None


# ════════════════════════════════════════════════════════════
# 字段语义比较（与 evaluate_m15.py 保持一致但更简化）
# ════════════════════════════════════════════════════════════

def _normalize_date(val: str) -> str:
    """多种日期格式统一成 YYYY-MM-DD"""
    import re
    from datetime import datetime as _dt

    if not val:
        return ""
    v = str(val).strip()

    candidates = ["%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y%m%d"]
    for fmt in candidates:
        try:
            return _dt.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # 宽松匹配
    nums = re.findall(r"\d+", v)
    if len(nums) >= 3:
        try:
            y, m, d = int(nums[0]), int(nums[1]), int(nums[2])
            if 1900 <= y <= 2100:
                return f"{y:04d}-{m:02d}-{d:02d}"
        except (ValueError, IndexError):
            pass
    return v


def _normalize_amount(val) -> float:
    """金额 → float"""
    if isinstance(val, (int, float)):
        return float(val)
    if not val:
        return 0.0
    import re
    v = str(val).strip()
    v = re.sub(r"[¥￥,，\s]", "", v)
    v = re.sub(r"人民币|元|RMB|CNY", "", v)
    mult = 1.0
    if v.endswith("万"):
        mult = 10000; v = v[:-1]
    try:
        return float(v) * mult
    except ValueError:
        return 0.0


def field_match(pred, gold, field_name: str) -> bool:
    """逐字段比较：日期归一化 / 金额 2% 容差 / 字符串双向包含"""
    if pred is None and gold is None:
        return True
    if pred is None or gold is None:
        return False

    # 日期类字段
    if "date" in field_name.lower() or "日期" in field_name:
        return _normalize_date(pred) == _normalize_date(gold)

    # 金额类字段
    if "amount" in field_name.lower() or "金额" in field_name:
        p_amt = _normalize_amount(pred)
        g_amt = _normalize_amount(gold)
        if g_amt == 0:
            return p_amt == 0
        return abs(p_amt - g_amt) / g_amt <= 0.02  # 2% 容差

    # 字符串双向包含
    p_str = str(pred).strip().lower()
    g_str = str(gold).strip().lower()
    if not p_str and not g_str:
        return True
    if not p_str or not g_str:
        return False
    return p_str == g_str or p_str in g_str or g_str in p_str


# ════════════════════════════════════════════════════════════
# AI 服务客户端
# ════════════════════════════════════════════════════════════

class AIServiceClient:
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip("/")

    def health(self) -> bool:
        if requests is None:
            return False
        try:
            resp = requests.get(f"{self.base_url}/api/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def extract_from_text(self, contract_text: str, contract_type: str = "purchase") -> dict:
        """直接从文本提取（不上传 PDF）"""
        if requests is None:
            raise RuntimeError("请先 pip install requests")

        resp = requests.post(
            f"{self.base_url}/api/contract/extract",
            json={
                "contract_text": contract_text,
                "contract_type": contract_type,
                "use_rag": False,  # 简化：不用 RAG
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


# ════════════════════════════════════════════════════════════
# 离线 mock（无 AI 服务时演示）
# ════════════════════════════════════════════════════════════

def mock_extract(contract_text: str, contract_type: str) -> dict:
    """简单关键词规则模拟 AI 提取结果，用于演示脚本输出格式"""
    result = {}
    result["contract_name"] = "XX 采购合同"
    result["contract_type"] = contract_type
    result["amount"] = 500000.0
    result["date_signed"] = "2026-08-01"
    result["date_start"] = "2026-08-15"
    result["date_end"] = "2027-08-14"
    result["party_a"] = "XX 科技有限公司"
    result["party_b"] = "YY 信息技术有限公司"
    result["payment_method"] = "银行转账"
    result["dispute_resolution"] = "诉讼"
    return result


# ════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════

def load_annotations(annot_dir: str) -> list[dict]:
    """加载所有标注 JSON"""
    annot_path = Path(annot_dir)
    results = []
    for f in sorted(annot_path.glob("*.json")):
        if f.name.startswith("_"):
            continue  # 跳过模板
        with open(f, encoding="utf-8") as fh:
            try:
                results.append(json.load(fh))
            except json.JSONDecodeError as e:
                print(f"⚠️  跳过损坏的 {f.name}: {e}")
    return results


def run_evaluation(
    annot_dir: str,
    ai_service_url: str = "http://localhost:8000",
    offline: bool = False,
) -> dict:
    """运行评估，返回结果字典"""
    annotations = load_annotations(annot_dir)
    if not annotations:
        print(f"❌ 在 {annot_dir} 中没找到标注 JSON")
        return {"error": "no annotations"}

    if not offline:
        client = AIServiceClient(ai_service_url)
        if not client.health():
            print(f"⚠️  AI 服务 {ai_service_url} 不可用，自动切换离线 mock 模式")
            offline = True

    field_stats: dict[str, {"total": int, "correct": int}] = {}
    per_contract: list[dict] = []

    for ann in annotations:
        # 兼容两种标注格式：
        #   A) M13 格式: {"fields": {...}, "contract_type": "..."}
        #   B) 扁平格式: {"contract_type": "采购合同", "amount": 580000, ...}
        if "fields" in ann:
            gold = ann.get("fields", {})
            contract_type = ann.get("contract_type", "purchase")
        else:
            # 扁平格式：跳过元信息 + 原文字段，其余全是 fields
            _META_KEYS = {
                "id", "contract_type", "contract_name", "contract_code",
                "_合同原文", "raw_text", "contract_text", "mock_text",
                "annotation", "source_file", "created_at",
            }
            contract_type_raw = ann.get("contract_type", "")
            # 兼容中文 → 英文
            _TYPE_MAP = {
                "采购合同": "purchase", "销售合同": "sales",
                "服务合同": "service", "租赁合同": "lease", "其他": "other",
            }
            contract_type = _TYPE_MAP.get(contract_type_raw, contract_type_raw or "purchase")
            gold = {k: v for k, v in ann.items() if k not in _META_KEYS}

        # 日期字段名兼容（标注用 sign_date/effective_date/expire_date，mock 用 date_signed/date_start/date_end）
        _FIELD_ALIAS = {
            "sign_date": "date_signed",
            "effective_date": "date_start",
            "expire_date": "date_end",
        }

        # 离线模式没有真实文本，用标注里的 mock 文本或直接用 mock
        text = ann.get("mock_text", f"这是一份{contract_type}示例合同...")

        if offline:
            pred = mock_extract(text, contract_type)
        else:
            try:
                resp = client.extract_from_text(text, contract_type)
                pred = resp.get("data", {}).get("extraction", {})
            except Exception as e:
                print(f"  ⚠️  API 调用失败: {e}")
                pred = {}

        contract_result = {"name": ann.get("contract_name", ann.get("id", "unknown")), "fields": {}}
        for field_name, gold_val in gold.items():
            # 尝试用别名查找 pred
            pred_key = _FIELD_ALIAS.get(field_name, field_name)
            pred_val = pred.get(field_name, pred.get(pred_key))
            match = field_match(pred_val, gold_val, field_name)

            if field_name not in field_stats:
                field_stats[field_name] = {"total": 0, "correct": 0}
            field_stats[field_name]["total"] += 1
            if match:
                field_stats[field_name]["correct"] += 1

            contract_result["fields"][field_name] = {
                "gold": gold_val, "pred": pred_val, "match": match,
            }

        per_contract.append(contract_result)

    # 汇总
    total = sum(s["total"] for s in field_stats.values())
    correct = sum(s["correct"] for s in field_stats.values())
    overall_acc = correct / total if total else 0.0

    return {
        "timestamp": datetime.now().isoformat(),
        "mode": "offline_mock" if offline else "live_api",
        "total_contracts": len(per_contract),
        "overall_accuracy": round(overall_acc, 4),
        "field_stats": {
            k: {
                "total": v["total"],
                "correct": v["correct"],
                "accuracy": round(v["correct"] / v["total"], 4) if v["total"] else 0,
            }
            for k, v in field_stats.items()
        },
        "per_contract": per_contract,
    }


def print_report(result: dict):
    """打印人类可读报告"""
    if "error" in result:
        print(f"❌ {result['error']}")
        return

    print("=" * 60)
    print(f"📊 智能合同字段准确率报告")
    print("=" * 60)
    print(f"  时间:       {result['timestamp']}")
    print(f"  模式:       {result['mode']}")
    print(f"  合同数:     {result['total_contracts']}")
    print(f"  总准确率:   {result['overall_accuracy']:.2%}")
    print("-" * 60)
    print("📋 逐字段准确率:")

    # 按准确率排序
    sorted_fields = sorted(
        result["field_stats"].items(),
        key=lambda x: x[1]["accuracy"],
    )
    for field, stats in sorted_fields:
        bar = "█" * int(stats["accuracy"] * 20) + "░" * (20 - int(stats["accuracy"] * 20))
        print(f"  {field:<20s} {bar} {stats['accuracy']:.0%} ({stats['correct']}/{stats['total']})")

    print("-" * 60)
    print(f"📁 逐合同详情（准确率）:")
    for c in result["per_contract"]:
        matches = sum(1 for f in c["fields"].values() if f["match"])
        total = len(c["fields"])
        pct = f"{matches/total:.0%}" if total > 0 else "N/A"
        print(f"  {c['name']:<30s} {matches}/{total} ({pct})")

    print("=" * 60)


def save_json(result: dict, output_dir: str = "reports"):
    """保存 JSON 原始结果"""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"accuracy_test_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n💾 报告已保存: {path}")


# ════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="智能合同字段准确率测试")
    parser.add_argument("--annot-dir", default="examples/annotations",
                        help="标注 JSON 目录（默认 examples/annotations）")
    parser.add_argument("--pdf-dir", default=None,
                        help="PDF 目录（可选，当前简化版只用标注文本）")
    parser.add_argument("--ai-url", default="http://localhost:8000",
                        help="AI 服务地址（默认 http://localhost:8000）")
    parser.add_argument("--offline", action="store_true",
                        help="离线 mock 模式（不调 LLM，用于演示）")
    parser.add_argument("--output-dir", default="reports",
                        help="JSON 报告输出目录")
    args = parser.parse_args()

    result = run_evaluation(
        annot_dir=args.annot_dir,
        ai_service_url=args.ai_url,
        offline=args.offline,
    )
    print_report(result)
    save_json(result, args.output_dir)

    # 返回码：准确率 < 0.6 视为失败（CI 可用）
    if result.get("overall_accuracy", 1.0) < 0.6 and not args.offline:
        sys.exit(1)


if __name__ == "__main__":
    main()

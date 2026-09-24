"""M18 · 演示步骤辅助脚本

打印一份标准演示流程的时间线，也可以让脚本自己逐步执行（带 pause）。

用法:
    python demo_script.py                # 只打印步骤（不执行）
    python demo_script.py --run          # 逐步执行（需 Docker 已启动）
    python demo_script.py --run --slow   # 慢速演示（每步停顿 3 秒）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


DEMO_STEPS = [
    # (序号, 时长估计, 标题, 详细操作, 执行命令)
    (1, "10s", "环境检查",
     "确认 Docker Desktop 运行中，四个容器 healthy",
     "docker compose ps"),

    (2, "5s", "打开 Odoo 系统",
     "浏览器访问 http://localhost:8069，用 admin 登录",
     None),

    (3, "30s", "创建第一份采购合同（手动）",
     "合同管理 → 合同台账 → 新建\n"
     "  合同名称: 服务器采购合同\n"
     "  合同类型: 采购合同\n"
     "  甲方/乙方: 任选档案中的相对方\n"
     "  金额: ¥500,000\n"
     "  付款条款: 预付款 30%，到货款 60%，质保金 10%\n"
     "  签订日期: 今天",
     None),

    (4, "15s", "自动生成收付款计划",
     "点击 header「📋 自动生成计划」按钮\n"
     "观察付款计划 Tab 自动填入 3 条记录\n"
     "（预付款 30% / 到货款 60% / 质保金 10%）",
     None),

    (5, "10s", "台账汇总视图",
     "合同管理 → 收付款台账\n"
     "切换到 Pivot 视图\n"
     "行维度: 合同类型 × 状态\n"
     "可看到合同金额的多维分析",
     None),

    (6, "10s", "配置系统参数",
     "合同管理 → 系统配置\n"
     "  编号前缀: HT-\n"
     "  日期格式: YYYYMM\n"
     "  预览: HT-2026090001\n"
     "  点击「💾 保存并应用」",
     None),

    (7, "5s", "新建合同，验证新编号规则",
     "新建一份合同 → 编号自动变为 HT-202609NNNN",
     None),

    (8, "10s", "导出 Excel 台账",
     "合同列表页 → 选中多条合同 → 点击「📤 导出 Excel」\n"
     "浏览器下载 .xlsx（含合同列表 + 按类型汇总两个 Sheet）",
     None),

    (9, "10s", "导出 PDF 台账/详情",
     "选中合同 → 顶部「打印」按钮 → 合同台账汇总 PDF / 合同详情 PDF",
     None),

    (10, "15s", "AI 服务健康检查",
     "Swagger UI: http://localhost:8000/docs\n"
     "GET /api/health → 看 LLM provider / embedding model",
     "curl http://localhost:8000/api/health"),

    (11, "10s", "跑准确率测试",
     "在 ai-service/ 目录执行：\n"
     "  python scripts/run_accuracy_test.py --offline\n"
     "观察字段准确率条形图输出",
     None),

    (12, "10s", "跑 pytest 单元测试",
     "cd ai-service && pytest tests/ -v",
     None),
]


def print_steps():
    """打印演示时间线"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_sec = sum(int(s[1].replace("s", "")) for s in DEMO_STEPS)

    print()
    print("=" * 72)
    print(f"🎬 智能合同管理系统 · 演示脚本")
    print(f"   生成时间: {now}")
    print(f"   预计总时长: {total_sec} 秒 (~{total_sec // 60} 分钟)")
    print("=" * 72)

    for seq, dur, title, action, cmd in DEMO_STEPS:
        print(f"\n{'─' * 72}")
        print(f"  [{seq:02d}] ⏱ 约 {dur}  │ {title}")
        print(f"{'─' * 72}")
        # 打印操作步骤（缩进）
        for line in action.strip().split("\n"):
            print(f"    {line}")
        if cmd:
            print(f"    📜 命令: {cmd}")

    print(f"\n{'=' * 72}")
    print("✅ 演示完成！")
    print("=" * 72)


def run_steps(slow: bool = False):
    """逐步执行（只有带命令的步骤会真执行）"""
    print_steps()
    print("\n\n🚀 开始逐步执行...\n")
    wait = 3 if slow else 1

    for seq, dur, title, action, cmd in DEMO_STEPS:
        print(f"\n▶ [{seq:02d}] {title} ({dur})")
        if cmd:
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    cwd=str(Path(__file__).resolve().parent.parent.parent),
                    timeout=30,
                )
                output = (result.stdout or result.stderr).strip()
                print(f"   执行: {cmd}")
                if output:
                    for line in output.split("\n")[:5]:
                        print(f"   │ {line}")
                    if len(output.split("\n")) > 5:
                        print(f"   │ ... (省略)")
                print(f"   退出码: {result.returncode}")
            except Exception as e:
                print(f"   ❌ 执行失败: {e}")
        else:
            print(f"   👉 请手动执行（UI 操作）")

        time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(description="演示步骤辅助脚本")
    parser.add_argument("--run", action="store_true",
                        help="逐步执行（只执行带命令的步骤）")
    parser.add_argument("--slow", action="store_true",
                        help="慢速模式（每步 3 秒）")
    args = parser.parse_args()

    if args.run:
        run_steps(slow=args.slow)
    else:
        print_steps()


if __name__ == "__main__":
    main()

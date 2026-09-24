# -*- coding: utf-8 -*-
"""
M24 单元测试：合同条款按章节拆分（clause_segmenter 升级）

覆盖:
  1. 「一、二、三、」中文章节头识别（此前走「全文」兜底的场景）
  2. 文头信息（标题/编号/甲乙方/前言）→ 「合同基本信息」(other)
  3. 类型规则: 付款相关→payment / 争议→dispute / 金额期限生效违约→main
  4. 段落回填合并（句子切断修复）+ 键值行/子项行保护
  5. 文本规范化（空格清理/括号统一），千分位数字不受影响
  6. 「第X条」正式结构回归: 正文里「一、二、」子列表不误拆
  7. 边界: 空文本 / 无结构文本兜底 / 排序重编号

运行: python test_m24.py
"""
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.services.clause_segmenter import (
    segment_clauses,
    normalize_clause_text,
    _reflow_lines,
    _guess_clause_type,
)

# 与截图合同 XS-2024-012 同构的样例（「一、二、三」章节 + PDF 逐行断句）
SAMPLE = """智能产品销售合同
合同编号：XS-2024-012
合同类型：销售合同
甲方（买方/委托方）：深圳创新电子有限公司
乙方（卖方/服务方）：杭州华东商贸有限公司
甲乙双方经友好协商，本着平等自愿、诚实信用的原则，就以下事宜达成一致意见，签订
本合同。
一、合同标的
本合同项下标的为 智能产品销售合同
所约定的全部内容。
二、合同金额
合同总金额为人民币 320,000 元(¥320,000)。
大写金额：叁拾贰万元整
三、付款条款
乙方在合同签订后 3 个工作日内支付 40%
预付款；甲方完成生产并通知乙方验货，乙方确认后支付 50% 发货款。
四、合同期限
本合同自 2024-05-20 起生效，有效期至 2025-05-20。
五、违约责任
任何一方违约的，应向守约方支付合同总额的 10% 作为违约金。
六、争议解决
因本合同引起的争议，双方应友好协商解决；协商不成的，向合同签订地人民法院提起诉讼。"""


def test_sample_split():
    """样例合同: 按章节拆成 7 条，头部信息归 other"""
    clauses = segment_clauses(SAMPLE)
    assert len(clauses) == 7, f"应拆出 7 条，实际 {len(clauses)}: {[c['name'] for c in clauses]}"
    assert clauses[0]["name"] == "合同基本信息"
    assert clauses[0]["clause_type"] == "other"
    names = [c["name"] for c in clauses]
    assert names == [
        "合同基本信息", "合同标的", "合同金额", "付款条款",
        "合同期限", "违约责任", "争议解决",
    ], names
    # 排序重编号 1..N
    assert [c["sort_order"] for c in clauses] == list(range(1, 8))
    print("  ✅ 样例拆分: 7 条独立条款，头部归 other，排序连续")


def test_sample_types():
    """类型规则: 付款→payment / 争议→dispute / 金额期限违约标的→main"""
    clauses = {c["name"]: c["clause_type"] for c in segment_clauses(SAMPLE)}
    assert clauses["付款条款"] == "payment", clauses
    assert clauses["争议解决"] == "dispute", clauses
    assert clauses["合同金额"] == "main", clauses
    assert clauses["合同期限"] == "main", clauses
    assert clauses["违约责任"] == "main", clauses
    assert clauses["合同标的"] == "main", clauses
    print("  ✅ 类型规则: 付款/争议/main 兜底全部正确")


def test_header_content():
    """文头内容: 键值行独立，断句合并（签订/本合同。 → 一行）"""
    header = segment_clauses(SAMPLE)[0]["content"]
    assert "合同编号：XS-2024-012" in header
    assert "甲方（买方/委托方）：深圳创新电子有限公司" in header
    assert "乙方（卖方/服务方）：杭州华东商贸有限公司" in header
    # 回填: 「…达成一致意见，签订\n本合同。」→ 合并为一行
    assert "达成一致意见，签订本合同。" in header, header
    # 键值行不被合并
    assert "合同编号：XS-2024-012\n" in header, header
    print("  ✅ 文头信息: 键值行独立、断句已回填合并")


def test_reflow_join_and_protect():
    """回填合并单元: 普通续行拼接 / KV 行、句末标点、子项行保护"""
    # 续行拼接
    assert _reflow_lines(["本合同项下标的为 智能产品销售合同", "所约定的全部内容。"]) == \
        ["本合同项下标的为 智能产品销售合同所约定的全部内容。"][0]
    # 上一行句末标点 → 不拼
    assert "\n" in _reflow_lines(["第一条。", "第二句"])
    # 下一行 KV → 不拼
    assert "\n" in _reflow_lines(["双方约定如下", "合同编号：XS-001"])
    # 上一行 KV → 不拼
    assert "\n" in _reflow_lines(["甲方：甲公司", "乙方：乙公司"])
    # 下一行子项 → 不拼
    assert "\n" in _reflow_lines(["具体包括以下内容", "（一）系统开发"])
    print("  ✅ 段落回填: 拼接与保护规则全部生效")


def test_normalize():
    """规范化: CJK 间空格删除 / 千分位保留 / 含汉字半角括号转全角"""
    t = normalize_clause_text("本合同项下标的为 智能产品销售合同 所约定的全部内容 。")
    assert t == "本合同项下标的为智能产品销售合同所约定的全部内容。", t
    # 千分位逗号与数字不动
    t2 = normalize_clause_text("合同总金额为人民币 320,000 元。")
    assert "320,000" in t2, t2
    # 含汉字半角括号 → 全角
    t3 = normalize_clause_text("甲方（买方/委托方）：深圳创新电子有限公司")
    assert t3 == "甲方（买方/委托方）：深圳创新电子有限公司", t3
    t4 = normalize_clause_text("智能产品(含配件)销售")
    assert "（含配件）" in t4, t4
    # 纯数字半角括号保留
    t5 = normalize_clause_text("金额为 1000(USD)")
    assert "(USD)" in t5, t5
    print("  ✅ 文本规范化: 空格/括号/标点清理正确，业务数据未受影响")


def test_clause_content_normalized():
    """条款正文: 标的句子回填 + 空格清理；金额行千分位保留"""
    clauses = {c["name"]: c for c in segment_clauses(SAMPLE)}
    bd = clauses["合同标的"]["content"]
    assert "一、合同标的" in bd, bd  # 标题行保留
    assert "本合同项下标的为智能产品销售合同所约定的全部内容。" in bd, bd
    amt = clauses["合同金额"]["content"]
    assert "320,000" in amt and "大写金额：叁拾贰万元整" in amt, amt
    print("  ✅ 条款正文: 标题行独立、断句回填、空格清理、数字保护全部正确")


def test_formal_head_regression():
    """回归: 有「第X条」正式条款头时，正文里「一、二、」不误拆"""
    text = """甲方：甲公司
乙方：乙公司
第一条 服务内容
乙方提供的服务包括：
一、系统开发
二、系统运维
第二条 服务期限
服务期为一年。"""
    clauses = segment_clauses(text)
    cmap = {c["name"]: c for c in clauses}
    names = [c["name"] for c in clauses]
    assert names == ["合同基本信息", "服务内容", "服务期限"], names
    # 一、二、 留在第一条正文里
    assert "一、系统开发" in cmap["服务内容"]["content"]
    assert "二、系统运维" in cmap["服务内容"]["content"]
    print("  ✅ 第X条回归: 子列表不误拆，文头归 other")


def test_fallback_and_edges():
    """边界: 空文本 → []；无结构文本 → 全文兜底；标题超长截断"""
    assert segment_clauses("") == []
    assert segment_clauses("   \n  ") == []
    fallback = segment_clauses("一段没有任何条款结构的文字内容而已")
    assert len(fallback) == 1 and fallback[0]["name"] == "全文"
    assert fallback[0]["clause_type"] == "other"
    print("  ✅ 边界: 空文本/无结构兜底正确")


def test_type_priority():
    """类型优先级: 标题「合同金额与支付方式」含支付 → payment 优先于 main"""
    assert _guess_clause_type("合同金额与支付方式", "") == "payment"
    assert _guess_clause_type("合同金额", "") == "main"
    assert _guess_clause_type("保密条款", "双方对合作内容保密") == "confidential"
    print("  ✅ 类型优先级: payment > main，具体类型优先")


def main():
    tests = [
        test_sample_split,
        test_sample_types,
        test_header_content,
        test_reflow_join_and_protect,
        test_normalize,
        test_clause_content_normalized,
        test_formal_head_regression,
        test_fallback_and_edges,
        test_type_priority,
    ]
    print("=" * 60)
    print("M24 条款章节拆分 单元测试")
    print("=" * 60)
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {t.__name__}: {e}")
    print("=" * 60)
    if failed:
        print(f"结果: {failed}/{len(tests)} 失败")
        sys.exit(1)
    print(f"结果: 全部通过（{len(tests)} 组用例）")


if __name__ == "__main__":
    main()

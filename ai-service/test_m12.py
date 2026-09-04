"""M12 · RAG 监督学习模块 — 本地 mock 测试（不连 Chroma）"""
import sys, os, json
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from app.services.rag_learner import RAGLearner


# ═══════════════════════════════════════════════════════════
# Mock 辅助
# ═══════════════════════════════════════════════════════════

def _make_mock_vector_store():
    """创建一个 mock VectorStore，记录调用但不做真实操作"""
    mock_vs = MagicMock()
    mock_vs.add_texts.return_value = ["id1", "id2"]
    mock_vs.count.return_value = 0
    mock_vs.list_collections.return_value = []
    return mock_vs


# ── 测试用范例数据 ──

SAMPLE_EXAMPLES = [
    {
        "id": "purchase_01",
        "contract_type": "采购合同",
        "text": "甲方（采购方）北京科技有限公司与乙方（供货方）上海贸易有限公司。"
                "经双方协商一致，甲方同意向乙方采购以下设备：服务器 10 台，单价 50,000 元。"
                "合同总金额人民币 500,000 元整。付款方式：货到验收合格后 30 日内付款。"
                "交货地点甲方指定仓库。交货期限：合同签订后 60 日内交付完毕。"
                "质量标准：乙方提供的设备须符合国家相关质量标准，质保期不少于一年。"
                "违约责任：甲方逾期付款的，应按逾期金额的日万分之五向乙方支付滞纳金。"
                "乙方逾期交货的，应按逾期交货金额的日万分之五向甲方支付违约金。"
                "争议解决方式：友好协商，协商不成向人民法院提起诉讼。本合同自双方盖章之日起生效。",
        "extraction": {
            "contract_name": "办公设备采购合同",
            "partner_a": "北京科技有限公司",
            "partner_b": "上海贸易有限公司",
            "amount": 500000.0,
            "payment_terms": "货到验收合格后 30 日内付款",
            "dispute_resolution": "诉讼",
        },
    },
    {
        "id": "sales_01",
        "contract_type": "销售合同",
        "text": "甲方（销售方）广州产品有限公司与乙方（买方）深圳贸易集团。"
                "甲方同意向乙方销售产品 A 1000 件，单价 100 元；产品 B 500 件，单价 200 元。"
                "合同总金额人民币 200,000 元整。买方应在合同签订后 10 日内支付 30% 预付款。"
                "剩余货款在甲方发货并提供增值税专用发票后 20 日内付清。"
                "交货时间：收到预付款后 30 日内完成发货。运费由甲方承担。"
                "质量保证：产品质量符合国家标准，质保期 12 个月。"
                "违约责任：任何一方违约应向守约方支付合同总金额 5% 的违约金。"
                "争议解决：协商不成，提交广州仲裁委员会仲裁。本合同一式两份，双方各执一份。",
        "extraction": {
            "contract_name": "产品销售合同",
            "partner_a": "广州产品有限公司",
            "partner_b": "深圳贸易集团",
            "amount": 200000.0,
            "payment_terms": "合同签订后 10 日内支付 30% 预付款",
            "dispute_resolution": "仲裁",
        },
    },
    {
        "id": "lease_01",
        "contract_type": "租赁合同",
        "text": "甲方（出租方）张某某与乙方（承租方）李四公司。"
                "甲方将位于北京市朝阳区 XX 大厦 15 层的办公场地出租给乙方使用。"
                "租期自 2025 年 1 月 1 日起至 2027 年 12 月 31 日止，共计 36 个月。"
                "租金为每月人民币 50,000 元整，按季度支付，每季度第一个月 5 日前支付。"
                "押金为两个月租金共计 100,000 元，于合同签订时一次性支付。"
                "水电费、物业费由乙方另行承担。承租方不得擅自转租或改变房屋用途。"
                "承租方逾期支付租金的，按逾期金额的日万分之三支付滞纳金。"
                "合同期满后乙方享有优先续租权。争议解决方式：向房屋所在地人民法院提起诉讼。",
        "extraction": {
            "contract_name": "房屋租赁合同",
            "partner_a": "张某某",
            "partner_b": "李四公司",
            "amount": 1800000.0,
            "payment_terms": "按季支付，每季度第一个月 5 日前支付",
            "dispute_resolution": "诉讼",
        },
    },
]


# ═══════════════════════════════════════════════════════════
# 测试 1 · add_example 基本流程
# ═══════════════════════════════════════════════════════════

def test_add_example_basic():
    print("📋 测试 1 · add_example 基本流程")

    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs, collection_name="contract_examples")

    result = learner.add_example(SAMPLE_EXAMPLES[0])

    # 验证 VectorStore.add_texts 被正确调用
    mock_vs.add_texts.assert_called_once()
    call_kwargs = mock_vs.add_texts.call_args.kwargs
    assert call_kwargs.get("collection_name") == "contract_examples"
    assert len(call_kwargs.get("texts", [])) > 0  # 切块后应有多个文本
    # metadata 应包含 example_id 和 contract_type
    metas = call_kwargs.get("metadatas", [])
    assert len(metas) > 0
    assert metas[0].get("example_id") == "purchase_01"
    assert metas[0].get("contract_type") == "采购合同"

    # 验证内存缓存更新
    assert "purchase_01" in learner._examples
    cached = learner._examples["purchase_01"]
    assert cached["contract_type"] == "采购合同"
    assert cached["extraction"]["partner_a"] == "北京科技有限公司"

    # 验证返回值
    assert result["example_id"] == "purchase_01"
    assert result["chunk_count"] > 0

    print(f"  ✅ add_texts 调用正确（{len(call_kwargs['texts'])} chunks）")
    print(f"  ✅ 内存缓存已更新（chunk_count={cached['chunk_count']}）")
    print(f"  ✅ 返回值正确: chunk_count={result['chunk_count']}")

    print("  🟢 测试 1 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 2 · add_example 字段校验
# ═══════════════════════════════════════════════════════════

def test_add_example_validation():
    print("📋 测试 2 · add_example 字段校验")

    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs)

    # 缺少 id
    try:
        learner.add_example({"contract_type": "采购合同", "text": "xxx"})
        assert False, "应该抛 ValueError"
    except ValueError as e:
        assert "缺少必填字段" in str(e)
        print(f"  ✅ 缺少 id → ValueError: {e}")

    # 缺少 contract_type
    try:
        learner.add_example({"id": "x", "text": "xxx"})
        assert False, "应该抛 ValueError"
    except ValueError as e:
        assert "缺少必填字段" in str(e)
        print(f"  ✅ 缺少 contract_type → ValueError: {e}")

    # 缺少 text
    try:
        learner.add_example({"id": "x", "contract_type": "采购合同"})
        assert False, "应该抛 ValueError"
    except ValueError as e:
        assert "缺少必填字段" in str(e)
        print(f"  ✅ 缺少 text → ValueError: {e}")

    print("  🟢 测试 2 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 3 · add_examples_batch 批量
# ═══════════════════════════════════════════════════════════

def test_add_examples_batch():
    print("📋 测试 3 · add_examples_batch 批量入库")

    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs)

    results = learner.add_examples_batch(SAMPLE_EXAMPLES)

    assert len(results) == 3
    assert all("error" not in r for r in results), "全部应成功"
    assert mock_vs.add_texts.call_count == 3  # 每个范例各调一次

    # 内存缓存应有 3 条
    assert len(learner._examples) == 3
    for eid in ["purchase_01", "sales_01", "lease_01"]:
        assert eid in learner._examples

    stats = learner.stats()
    assert stats["total_examples"] == 3
    assert stats["by_contract_type"]["采购合同"] == 1
    assert stats["by_contract_type"]["销售合同"] == 1
    assert stats["by_contract_type"]["租赁合同"] == 1

    print(f"  ✅ 3/3 成功入库")
    print(f"  ✅ stats: examples={stats['total_examples']}, by_type={stats['by_contract_type']}")

    print("  🟢 测试 3 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 4 · retrieve_examples 无类型过滤
# ═══════════════════════════════════════════════════════════

def test_retrieve_examples_basic():
    print("📋 测试 4 · retrieve_examples 基本检索（无类型过滤）")

    # 先入库 3 个范例建立内存缓存
    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs)
    learner.add_examples_batch(SAMPLE_EXAMPLES)

    # 设置 VS.query 返回模拟 chunk 结果（模拟 purchase_01 的 chunks 排前列）
    mock_vs.query.return_value = [
        {"id": "purchase_01:0", "text": "合同总金额人民币 500,000 元整",
         "metadata": {"example_id": "purchase_01", "contract_type": "采购合同", "chunk_index": 0},
         "distance": 0.15, "similarity": 0.85},
        {"id": "purchase_01:1", "text": "付款方式：货到验收合格后 30 日内付款",
         "metadata": {"example_id": "purchase_01", "contract_type": "采购合同", "chunk_index": 1},
         "distance": 0.20, "similarity": 0.80},
        {"id": "sales_01:0", "text": "合同总金额人民币 100,000 元整",
         "metadata": {"example_id": "sales_01", "contract_type": "销售合同", "chunk_index": 0},
         "distance": 0.35, "similarity": 0.65},
    ]

    results = learner.retrieve_examples("付款方式和合同金额", top_k=2)

    assert len(results) == 2
    # Top1 应该是 purchase_01（有两个高相似度 chunk → 最佳 0.85）
    assert results[0]["id"] == "purchase_01"
    assert results[0]["contract_type"] == "采购合同"
    assert results[0]["score"] == 0.85  # 最高 chunk 分数
    # 应包含完整 extraction
    assert results[0]["extraction"]["amount"] == 500000.0
    # 应包含 matched_chunk
    assert "500,000" in results[0]["matched_chunk"] or "500000" in results[0]["matched_chunk"].replace(",", "")
    # Top2 是 sales_01
    assert results[1]["id"] == "sales_01"
    assert results[1]["score"] == 0.65

    # 验证 VectorStore.query 没传 where 过滤
    mock_vs.query.assert_called_once()
    call_kwargs = mock_vs.query.call_args.kwargs
    assert call_kwargs.get("filter") is None
    print(f"  ✅ Top1={results[0]['id']}(score={results[0]['score']}), Top2={results[1]['id']}(score={results[1]['score']})")
    print(f"  ✅ 无类型过滤: filter=None")
    print(f"  ✅ 返回 extraction 完整: amount={results[0]['extraction']['amount']}")

    print("  🟢 测试 4 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 5 · retrieve_examples 有类型过滤
# ═══════════════════════════════════════════════════════════

def test_retrieve_examples_with_filter():
    print("📋 测试 5 · retrieve_examples 带 contract_type 过滤")

    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs)
    learner.add_examples_batch(SAMPLE_EXAMPLES)

    mock_vs.query.return_value = [
        {"id": "purchase_01:0", "text": "付款方式",
         "metadata": {"example_id": "purchase_01", "contract_type": "采购合同"},
         "distance": 0.18, "similarity": 0.82},
    ]

    results = learner.retrieve_examples("付款方式", contract_type="采购合同", top_k=3)

    # 验证 where 过滤被正确传给 VectorStore.query
    mock_vs.query.assert_called_once()
    call_kwargs = mock_vs.query.call_args.kwargs
    assert call_kwargs.get("filter") == {"contract_type": "采购合同"}, \
        f"filter 应为 {{contract_type: 采购合同}}，实际 {call_kwargs.get('filter')}"
    print(f"  ✅ contract_type='采购合同' → where filter={{'contract_type': '采购合同'}}")

    assert len(results) == 1
    assert results[0]["id"] == "purchase_01"
    assert results[0]["contract_type"] == "采购合同"
    print(f"  ✅ 只返回采购合同范例: {results[0]['id']}(score={results[0]['score']})")

    print("  🟢 测试 5 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 6 · retrieve_examples 空结果 / 空查询
# ═══════════════════════════════════════════════════════════

def test_retrieve_examples_edge_cases():
    print("📋 测试 6 · retrieve_examples 边界情况")

    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs)

    # 空查询 → 直接返回空列表
    results = learner.retrieve_examples("")
    assert results == []
    print(f"  ✅ 空 query_text → []")

    results = learner.retrieve_examples("   ")
    assert results == []
    print(f"  ✅ 纯空格 query_text → []")

    # VS.query 返回空列表
    mock_vs.query.return_value = []
    results = learner.retrieve_examples("有内容但集合空")
    assert results == []
    print(f"  ✅ VS.query 返回空 → []")

    # VS.query 返回异常
    mock_vs.query.side_effect = Exception("Chroma 挂了")
    results = learner.retrieve_examples("查询但服务异常")
    assert results == []  # 不抛异常，返回空列表
    print(f"  ✅ VS.query 异常 → []（不中断）")

    print("  🟢 测试 6 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 7 · 范例去重（多 chunk → 单个范例）
# ═══════════════════════════════════════════════════════════

def test_retrieve_dedup_by_example_id():
    print("📋 测试 7 · 多 chunk 命中同一范例 → 按 example_id 去重")

    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs)
    learner.add_examples_batch(SAMPLE_EXAMPLES)

    # 模拟查询返回多个 chunks 来自同一范例（实际 RAG 常见场景）
    mock_vs.query.return_value = [
        {"id": "purchase_01:0", "text": "chunk0",
         "metadata": {"example_id": "purchase_01", "contract_type": "采购合同"},
         "distance": 0.10, "similarity": 0.90},
        {"id": "purchase_01:1", "text": "chunk1",
         "metadata": {"example_id": "purchase_01", "contract_type": "采购合同"},
         "distance": 0.12, "similarity": 0.88},  # 同一范例的另一个 chunk
        {"id": "purchase_01:2", "text": "chunk2",
         "metadata": {"example_id": "purchase_01", "contract_type": "采购合同"},
         "distance": 0.15, "similarity": 0.85},  # 又是同一范例
        {"id": "sales_01:0", "text": "chunk3",
         "metadata": {"example_id": "sales_01", "contract_type": "销售合同"},
         "distance": 0.30, "similarity": 0.70},
    ]

    results = learner.retrieve_examples("查询", top_k=5)

    # 应该只返回 2 个唯一范例（不是 4 个 chunks）
    assert len(results) == 2, f"应去重返回 2 个范例，实际 {len(results)}"
    assert results[0]["id"] == "purchase_01"
    # 分数应该是该范例所有 chunks 中最高的那个
    assert results[0]["score"] == 0.90
    assert results[0]["matched_chunk"] == "chunk0"  # 最佳 chunk

    print(f"  ✅ 4 chunks → 2 个唯一范例（按 example_id 去重）")
    print(f"  ✅ purchase_01 取最佳 chunk 分数 0.90（不是平均或最后一个）")

    print("  🟢 测试 7 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 8 · 工具方法（get_example / list_examples / stats）
# ═══════════════════════════════════════════════════════════

def test_util_methods():
    print("📋 测试 8 · 工具方法")

    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs)

    # 空状态
    assert learner.get_example("purchase_01") is None
    assert learner.list_examples() == []
    stats = learner.stats()
    assert stats["total_examples"] == 0
    print(f"  ✅ 空状态: get_example=None, list=[], stats.total=0")

    # 入库后
    learner.add_examples_batch(SAMPLE_EXAMPLES)

    ex = learner.get_example("purchase_01")
    assert ex is not None
    assert ex["contract_type"] == "采购合同"
    assert ex["extraction"]["amount"] == 500000.0
    print(f"  ✅ get_example: purchase_01 → contract_type={ex['contract_type']}, extraction.amount={ex['extraction']['amount']}")

    all_examples = learner.list_examples()
    assert len(all_examples) == 3
    print(f"  ✅ list_examples() → {len(all_examples)} 个范例")

    purchase_only = learner.list_examples(contract_type="采购合同")
    assert len(purchase_only) == 1
    assert purchase_only[0]["id"] == "purchase_01"
    print(f"  ✅ list_examples(contract_type='采购合同') → {len(purchase_only)} 个")

    stats = learner.stats()
    assert stats["total_examples"] == 3
    assert "采购合同" in stats["by_contract_type"]
    print(f"  ✅ stats: {stats['total_examples']} 范例, collection='{stats['collection_name']}'")

    # clear_memory
    learner.clear_memory()
    assert learner._examples == {}
    assert learner.get_example("purchase_01") is None
    print(f"  ✅ clear_memory() → 缓存清空")

    print("  🟢 测试 8 通过\n")


# ═══════════════════════════════════════════════════════════
# 测试 9 · load_examples() 从真实目录加载标注
# ═══════════════════════════════════════════════════════════

def test_load_examples_from_dir():
    print("📋 测试 9 · load_examples() 从真实目录加载标注 JSON")

    import tempfile
    from pathlib import Path

    mock_vs = _make_mock_vector_store()
    learner = RAGLearner(vector_store=mock_vs)

    # 创建临时目录，写几个 mock 标注 JSON
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # 写入 _TEMPLATE.json（应该被跳过）
        (tmp / "_TEMPLATE.json").write_text(
            '{"id": "should_skip", "contract_type": "采购合同"}', encoding="utf-8"
        )

        # 写入有效标注
        (tmp / "purchase_01.json").write_text(json.dumps({
            "id": "purchase_01",
            "contract_type": "采购合同",
            "contract_name": "办公设备采购合同",
            "partner_a": "北京科技有限公司",
            "partner_b": "上海贸易有限公司",
            "amount": 500000.0,
            "_备注": "这是一段说明文字，不应进入 extraction",
        }), encoding="utf-8")

        (tmp / "sales_01.json").write_text(json.dumps({
            "id": "sales_01",
            "contract_type": "销售合同",
            "contract_name": "产品销售合同",
            "partner_a": "广州产品有限公司",
            "partner_b": "深圳贸易集团",
            "amount": 200000.0,
        }), encoding="utf-8")

        # 写入缺少 id 的标注（应该被跳过）
        (tmp / "bad.json").write_text(json.dumps({
            "contract_type": "采购合同",  # 没有 id
            "contract_name": "坏合同",
        }), encoding="utf-8")

        # texts dict — 给两个有效范例提供文本
        texts = {
            "purchase_01": "这是一份完整的采购合同文本。甲方北京科技有限公司同意向乙方上海贸易有限公司采购办公设备。"
                           "合同总金额人民币 500,000 元整。付款方式：货到验收合格后 30 日内付款。"
                           "交货期限：合同签订后 60 日内交付完毕。质量标准：设备须符合国家相关质量标准，质保期不少于一年。"
                           "违约责任：甲方逾期付款按逾期金额的日万分之五支付滞纳金；乙方逾期交货按逾期金额的日万分之五支付违约金。"
                           "争议解决方式：友好协商，协商不成向人民法院提起诉讼。本合同自双方盖章之日起生效。合同一式两份，双方各执一份。",
            "sales_01": "这是一份完整的销售合同文本。甲方广州产品有限公司同意向乙方深圳贸易集团销售产品 A 和 B。"
                        "合同总金额人民币 200,000 元整。买方应在合同签订后 10 日内支付 30% 预付款。"
                        "剩余货款在甲方发货并提供增值税专用发票后 20 日内付清。"
                        "交货时间：收到预付款后 30 日内完成发货，运费由甲方承担。质量保证：产品质量符合国家标准，质保期 12 个月。"
                        "违约责任：任何一方违约应向守约方支付合同总金额 5% 的违约金。"
                        "争议解决：协商不成，提交广州仲裁委员会仲裁。本合同一式两份，双方各执一份，自双方盖章之日起生效。",
        }

        # 执行加载
        results = learner.load_examples(tmp, texts=texts)

    # 验证：_TEMPLATE.json 和 bad.json 被跳过，2 个有效范例成功
    success = [r for r in results if "error" not in r]
    assert len(success) == 2, f"应有 2 个成功，实际 {len(success)}"
    print(f"  ✅ 有效加载 2/4（_TEMPLATE + bad 被跳过）")

    # 验证内存缓存
    assert len(learner._examples) == 2
    assert "purchase_01" in learner._examples
    assert "sales_01" in learner._examples

    # 验证 extraction 正确过滤（去掉 _开头 和 id/contract_type）
    purchase = learner._examples["purchase_01"]
    assert "_备注" not in purchase["extraction"], "_开头的字段不应进入 extraction"
    assert "id" not in purchase["extraction"], "id 不应进入 extraction"
    assert "contract_type" not in purchase["extraction"], "contract_type 不应进入 extraction"
    assert purchase["extraction"]["contract_name"] == "办公设备采购合同"
    assert purchase["extraction"]["amount"] == 500000.0
    print(f"  ✅ extraction 正确过滤: {list(purchase['extraction'].keys())}")

    # 验证 VS.add_texts 被调用 2 次（每个范例一次）
    assert mock_vs.add_texts.call_count == 2
    print(f"  ✅ VS.add_texts 被调用 2 次（每个有效范例一次）")

    print("  🟢 测试 9 通过\n")


# ═══════════════════════════════════════════════════════════
# 运行全部测试
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("M12 · RAG 监督学习模块 — 本地 mock 测试")
    print("=" * 60 + "\n")

    test_add_example_basic()
    test_add_example_validation()
    test_add_examples_batch()
    test_retrieve_examples_basic()
    test_retrieve_examples_with_filter()
    test_retrieve_examples_edge_cases()
    test_retrieve_dedup_by_example_id()
    test_util_methods()
    test_load_examples_from_dir()

    print("=" * 60)
    print("🎉 全部 9 项测试通过 ✅")
    print("=" * 60)

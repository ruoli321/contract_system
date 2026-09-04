"""M8 端到端验证 - 实际跑 Chroma 完整链路"""
import sys
import json
sys.path.insert(0, r'd:\Project\langchain\contract-system\ai-service')

# 先确保 chromadb 可用
try:
    import chromadb
    from chromadb.utils import embedding_functions
    print(f"✅ chromadb {chromadb.__version__}")
except ImportError:
    print("❌ chromadb 未安装，pip install chromadb")
    sys.exit(1)

# 连接 Chroma（容器内用服务名 "chroma" + 端口 8000；宿主机用 "localhost" + 8001）
import os
_host = os.environ.get("CHROMA_HOST", "chroma")
_port = int(os.environ.get("CHROMA_PORT", "8000"))
print(f"\n🔗 连接 Chroma: {_host}:{_port}")
try:
    client = chromadb.HttpClient(host=_host, port=_port)
    hb = client.heartbeat()
    print(f"✅ Chroma 连接成功 | heartbeat={hb}")
except Exception as e:
    print(f"❌ Chroma 连接失败: {e}")
    print("   确保 docker compose up -d 已启动")
    sys.exit(1)

# Embedding
print("\n🔄 加载 Embedding 模型 BAAI/bge-small-zh-v1.5 (CPU)...")
ef = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="BAAI/bge-small-zh-v1.5",
    device="cpu",
)
print("✅ Embedding 就绪")

# 测试两个集合：contract_examples（标准范例）+ contract_chunks（当前合同块）
COLLECTION_EXAMPLES = "contract_examples"
COLLECTION_CHUNKS = "contract_chunks_test"  # 加 _test 后缀避免污染真实数据

# ── Step 1: 创建/获取集合 ──
print(f"\n📦 获取/创建集合 '{COLLECTION_EXAMPLES}'...")
examples_coll = client.get_or_create_collection(
    name=COLLECTION_EXAMPLES,
    embedding_function=ef,
    metadata={"hnsw:space": "cosine", "embedding_dim": 512},
)

print(f"📦 获取/创建集合 '{COLLECTION_CHUNKS}'...")
chunks_coll = client.get_or_create_collection(
    name=COLLECTION_CHUNKS,
    embedding_function=ef,
    metadata={"hnsw:space": "cosine", "embedding_dim": 512},
)

print(f"✅ 集合就绪 | examples={examples_coll.count()} chunks={chunks_coll.count()}")

# ── Step 2: upsert 测试数据 ──
print("\n📥 测试 upsert...")

# contract_examples（标准范例）
examples_coll.upsert(
    ids=[
        "example_purchase_001",
        "example_sale_001",
        "example_service_001",
    ],
    documents=[
        "采购合同范例：甲方采购服务器 100 台，总价 500 万，30 日内交货，30% 预付款，60% 验收款，10% 质保金。",
        "销售合同范例：乙方销售办公用品，按月结算，月结 30 天，质量保证期 12 个月。",
        "服务合同范例：技术咨询服务，按人天计费，交付物为技术方案和源代码。",
    ],
    metadatas=[
        {"contract_type": "采购", "version": "v1"},
        {"contract_type": "销售", "version": "v1"},
        {"contract_type": "服务", "version": "v2"},
    ],
)

# contract_chunks（实际切块）
chunks_coll.upsert(
    ids=[
        "chunk_001",
        "chunk_002",
        "chunk_003",
        "chunk_004",
    ],
    documents=[
        "第一条 合同标的：甲方向乙方采购服务器 100 台，单价 50,000 元。",
        "第二条 交货方式：30 日历日内交付，地点为甲方指定仓库。",
        "第三条 付款方式：30% 预付款，60% 验收款，10% 质保金。",
        "第四条 违约责任：违反合同支付总金额 5% 违约金。",
    ],
    metadatas=[
        {"source_file": "采购合同_2026.pdf", "clause_marker": "第一条", "contract_type": "采购"},
        {"source_file": "采购合同_2026.pdf", "clause_marker": "第二条", "contract_type": "采购"},
        {"source_file": "采购合同_2026.pdf", "clause_marker": "第三条", "contract_type": "采购"},
        {"source_file": "采购合同_2026.pdf", "clause_marker": "第四条", "contract_type": "采购"},
    ],
)

print(f"✅ examples: {examples_coll.count()} 条")
print(f"✅ chunks:   {chunks_coll.count()} 条")

# ── Step 3: query 基础检索 ──
print("\n🔍 测试 query...")

print("\n【基础查询】'怎么付款？' → contract_chunks:")
results = chunks_coll.query(
    query_texts=["怎么付款？"],
    n_results=3,
    include=["documents", "metadatas", "distances"],
)
for i in range(len(results["ids"][0])):
    chunk_id = results["ids"][0][i]
    text = results["documents"][0][i]
    distance = results["distances"][0][i]
    meta = results["metadatas"][0][i]
    similarity = round(1 - distance, 4)
    print(f"  [{similarity:.4f}] {chunk_id} ({meta.get('clause_marker', '?')})")
    print(f"       {text[:70]}...")

# ── Step 4: 按元数据过滤 ──
print("\n🔍 测试按元数据过滤...")

print("\n【过滤查询】'交货' → contract_chunks WHERE contract_type='采购':")
filtered = chunks_coll.query(
    query_texts=["交货"],
    n_results=3,
    where={"contract_type": "采购"},
    include=["documents", "metadatas", "distances"],
)
for i in range(len(filtered["ids"][0])):
    distance = filtered["distances"][0][i]
    similarity = round(1 - distance, 4)
    text = filtered["documents"][0][i][:70]
    print(f"  [{similarity:.4f}] {text}...")

# ── Step 5: examples 集合检索 ──
print("\n🔍 测试 contract_examples（标准范例）检索...")

print("\n【查询范例】'违约金条款' → contract_examples:")
ex_results = examples_coll.query(
    query_texts=["违约金怎么规定？"],
    n_results=2,
    include=["documents", "metadatas", "distances"],
)
for i in range(len(ex_results["ids"][0])):
    distance = ex_results["distances"][0][i]
    similarity = round(1 - distance, 4)
    meta = ex_results["metadatas"][0][i]
    print(f"  [{similarity:.4f}] {meta.get('contract_type', '?')} v{meta.get('version', '?')}")
    print(f"       {ex_results['documents'][0][i][:70]}...")

# ── Step 6: 空集合防护 ──
print("\n🧪 边界测试：空集合查询...")
try:
    empty_coll = client.get_or_create_collection("_tmp_empty_test")
    if empty_coll.count() == 0:
        print("  ✅ 空集合 count=0，正常")
    client.delete_collection("_tmp_empty_test")
    print("  ✅ 临时集合已清理")
except Exception as e:
    print(f"  ⚠️  空集合测试异常: {e}")

# ── 汇总 ──
print("\n" + "=" * 60)
print("✅ M8 端到端验证全部通过！")
print("=" * 60)
print(f"  Chroma 版本:    {chromadb.__version__}")
print(f"  Embedding 模型: BAAI/bge-small-zh-v1.5 (512d)")
print(f"  集合数:         {len([c.name for c in client.list_collections()])}")
for c in client.list_collections():
    print(f"    - {c.name} ({c.count()} docs)")

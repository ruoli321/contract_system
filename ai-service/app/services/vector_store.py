# ═══════════════════════════════════════════════════════════════════
# M8 · Embedding 与向量库管理
#
# VectorStore 类：Chroma 独立服务 + SentenceTransformer Embedding
#
# 设计要点（踩坑经验 1064430）：
#   1. Embedding 模型名称 + 维度写入 collection metadata，确保后续查询维度一致
#   2. 集合创建后维度固定，add/query 时自动校验
#   3. 默认 BAAI/bge-small-zh-v1.5（512 维，加载快，中文效果好）
#      升级 large 或 base 需改 embedding_model 配置 + 删旧集合重建
#   4. HttpClient 跨容器访问（Chroma 由 docker-compose 独立编排）
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger("vector-store")


class VectorStore:
    """
    Chroma 向量库管理类（通用，不带业务逻辑）

    用法:
        vs = VectorStore(host="chroma", port=8000)
        vs.add_texts(["合同条款 1", "合同条款 2"], metadatas=[{...}, {...}])
        results = vs.query("怎么付款？", top_k=3)
    """

    # ── 构造 ──────────────────────────────────────────────────────

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8001,
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        embedding_dimension: int = 512,
        device: str = "cpu",
        max_retries: int = 5,
        retry_interval: float = 3.0,
    ):
        """
        Args:
            host:                 Chroma 服务地址（容器内用 "chroma"，本地 "localhost"）
            port:                 Chroma 服务端口（容器内 8000，宿主机映射 8001）
            embedding_model:      SentenceTransformer 模型名
                                  默认 BAAI/bge-small-zh-v1.5（512 维）
                                  可选: BAAI/bge-large-zh-v1.5（1024 维）、bge-base-zh-v1.5（768 维）
            embedding_dimension:  向量维度（必须与模型匹配！）
            device:               "cpu" | "cuda"（GPU 加速）
            max_retries:          Chroma 连接重试次数
            retry_interval:       重试间隔秒数
        """
        self.host = host
        self.port = port
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension
        self.device = device
        self.max_retries = max_retries
        self.retry_interval = retry_interval

        # 延迟初始化（首次 add/query 时才连 Chroma + 加载模型）
        self._client = None
        self._embedding_fn = None
        self._collections = {}  # name → Collection（带 embedding_function 缓存）

        logger.info(
            f"📦 VectorStore 配置 | "
            f"服务: http://{host}:{port} | "
            f"模型: {embedding_model} ({embedding_dimension}d) | "
            f"设备: {device}"
        )

    # ── 底层初始化（延迟 + 重试）─────────────────────────────────

    def _ensure_ready(self):
        """确保 Chroma 客户端 + Embedding 函数已就绪"""
        if self._client is not None:
            return

        import chromadb
        from chromadb.utils import embedding_functions

        url = f"http://{self.host}:{self.port}"

        # 1) Chroma HttpClient（带重试）
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self._client = chromadb.HttpClient(host=self.host, port=self.port)
                self._client.heartbeat()
                logger.info(f"✅ Chroma 连接成功 ({url}, 尝试 {attempt})")
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    f"⚠️  Chroma 连接失败 ({attempt}/{self.max_retries}): {e}"
                    + (f" — {self.retry_interval}s 后重试..." if attempt < self.max_retries else "")
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_interval)

        if self._client is None:
            raise ConnectionError(
                f"无法连接 Chroma {url}，已重试 {self.max_retries} 次。"
                f" 最后错误: {last_err}"
            )

        # 2) SentenceTransformer Embedding Function
        #    Chroma 的 SentenceTransformerEmbeddingFunction 会自动下载模型到缓存
        #    容器内首次加载需要时间（约 30-60s），后续走本地缓存
        logger.info(f"🔄 加载 Embedding 模型 {self.embedding_model} ({self.device})...")
        self._embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=self.embedding_model,
            device=self.device,
        )
        logger.info(f"✅ Embedding 模型就绪")

    def _get_collection(self, name: str):
        """
        获取或创建集合（带维度一致性校验）

        关键：首次创建集合时把 model_name + embedding_dim 写入 metadata。
        后续 add/query 时如果现有集合维度与当前 embedding_dimension 不匹配，
        会在 Chroma 层直接报错。我们提前检查并给出明确错误提示。
        """
        self._ensure_ready()

        # 缓存命中
        if name in self._collections:
            return self._collections[name]

        # 获取或创建
        collection = self._client.get_or_create_collection(
            name=name,
            embedding_function=self._embedding_fn,
            metadata={
                "hnsw:space": "cosine",  # 余弦距离（语义相似度标准）
                "embedding_model": self.embedding_model,
                "embedding_dimension": self.embedding_dimension,
            },
        )

        # 维度校验（经验 1064430：集合用 bge-large 创建后，
        #  如果 VectorStore 换了 bge-small 就会报 768 vs 512）
        meta = collection.metadata or {}
        stored_dim = meta.get("embedding_dimension")
        if stored_dim and stored_dim != self.embedding_dimension:
            raise ValueError(
                f"集合 '{name}' 是用 {meta.get('embedding_model', '未知')} "
                f"({stored_dim}d) 创建的，但当前 VectorStore 配置的是 "
                f"{self.embedding_model} ({self.embedding_dimension}d)。"
                f"\n→ 解决：删旧集合重建 `vs.delete_collection('{name}')`"
            )

        self._collections[name] = collection
        logger.info(f"✅ 集合 '{name}' 就绪 | 当前文档数: {collection.count()}")
        return collection

    # ── 核心 API ──────────────────────────────────────────────────

    def add_texts(
        self,
        texts: list[str],
        metadatas: Optional[list[dict]] = None,
        collection_name: str = "contracts",
        ids: Optional[list[str]] = None,
    ) -> list[str]:
        """
        文本向量化并存入指定集合

        Args:
            texts:           文本列表（每个元素 = 一个向量单元）
            metadatas:       元数据列表（与 texts 一一对应）
                            推荐字段: source_file, clause_marker, contract_type, char_count
                            注意：值必须是 str/int/float/bool，Chroma 不支持嵌套 dict
            collection_name: 目标集合名（不存在则自动创建）
            ids:             自定义 ID（不提供则 Chroma 自动生成 UUID）

        Returns:
            实际使用的 ID 列表
        """
        if not texts:
            return []

        self._ensure_ready()
        collection = self._get_collection(collection_name)

        # 自动生成 ID
        if ids is None:
            import uuid
            ids = [str(uuid.uuid4()) for _ in texts]

        # 规范化 metadatas（确保值可 JSON 序列化）
        if metadatas is None:
            metadatas = [{} for _ in texts]
        # Chroma 不接受值为 None 的 metadata key —— 过滤掉
        metadatas = [
            {k: str(v) if isinstance(v, bool) else v
             for k, v in m.items()
             if v is not None}
            for m in metadatas
        ]

        try:
            collection.upsert(ids=ids, documents=texts, metadatas=metadatas)
            count = collection.count()
            logger.info(
                f"📥 入库 {len(texts)} 个文本 → 集合 '{collection_name}' | "
                f"累计 {count} 条"
            )
        except Exception as e:
            logger.error(f"入库失败: {e}")
            raise

        return ids

    def query(
        self,
        query_text: str,
        collection_name: str = "contracts",
        top_k: int = 5,
        filter: Optional[dict] = None,
    ) -> list[dict]:
        """
        相似检索：给定一段文本，返回 top_k 最相似的向量

        Args:
            query_text:      查询文本（会自动 Embedding）
            collection_name: 查询目标集合
            top_k:           返回条数
            filter:          元数据过滤（Chroma where 语法）
                            例: {"contract_type": "采购合同"}
                            例: {"$or": [{"contract_type": "采购合同"}, {"contract_type": "销售合同"}]}
                            例: {"char_count": {"$gte": 100}}

        Returns:
            [
                {
                    "id": "abc-123",
                    "text": "合同条款...",
                    "metadata": {"source_file": "xxx.pdf", ...},
                    "distance": 0.1234,     # cosine distance（越小越相似）
                    "similarity": 0.8766,   # 转换为相似度（1 - distance）
                },
                ...
            ]
        """
        self._ensure_ready()
        collection = self._get_collection(collection_name)

        # 要多少捞多少，但不超过集合总数
        actual_k = min(top_k, collection.count())
        if actual_k == 0:
            logger.warning(f"集合 '{collection_name}' 为空，无法查询")
            return []

        try:
            results = collection.query(
                query_texts=[query_text],
                n_results=actual_k,
                where=filter,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            logger.error(f"查询失败: {e}")
            raise

        # 格式转换：从 Chroma 嵌套列表 → 平铺的 dict 列表
        output = []
        if results["ids"] and results["ids"][0]:
            for i, chunk_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][i] if results["distances"] else 0.0
                output.append({
                    "id": chunk_id,
                    "text": results["documents"][0][i],
                    "metadata": results["metadatas"][0][i] or {},
                    "distance": round(distance, 4),
                    "similarity": round(1 - distance, 4),  # cosine distance → 相似度
                })

        logger.info(
            f"🔍 查询集合 '{collection_name}' → {len(output)} 条结果"
            + (f" | 过滤: {filter}" if filter else "")
            + (f" | 最高相似度: {output[0]['similarity']}" if output else "")
        )
        return output

    # ── 实用 API（管理用）──────────────────────────────────────

    def delete(
        self,
        ids: Optional[list[str]] = None,
        collection_name: str = "contracts",
        filter: Optional[dict] = None,
    ):
        """
        按 ID 或元数据条件删除

        Args:
            ids:             指定要删的 ID 列表
            filter:          按元数据条件批量删除（Chroma where 语法）
        """
        self._ensure_ready()
        collection = self._get_collection(collection_name)
        collection.delete(ids=ids, where=filter)
        logger.info(f"🗑️  删除 from '{collection_name}' | ids={ids} | filter={filter}")

    def count(
        self,
        collection_name: str = "contracts",
        filter: Optional[dict] = None,
    ) -> int:
        """统计集合文档数（可带 filter）"""
        self._ensure_ready()
        collection = self._get_collection(collection_name)
        if filter:
            return collection.get(where=filter, include=[])["ids"].__len__()
        return collection.count()

    def list_collections(self) -> list[str]:
        """列出所有集合名"""
        self._ensure_ready()
        return [c.name for c in self._client.list_collections()]

    def delete_collection(self, collection_name: str):
        """删除整个集合（危险操作，不可恢复）"""
        self._ensure_ready()
        self._client.delete_collection(collection_name)
        self._collections.pop(collection_name, None)
        logger.warning(f"💥 集合 '{collection_name}' 已删除")

    def get_collection_meta(self, collection_name: str) -> dict:
        """查看集合元信息（模型名、维度、文档数）"""
        self._ensure_ready()
        try:
            collection = self._client.get_collection(collection_name)
        except Exception:
            return {"error": f"集合 '{collection_name}' 不存在"}
        return {
            "name": collection.name,
            "count": collection.count(),
            "metadata": collection.metadata,
        }


# ─────────────────────────────────────────────────────────────────────
# 便捷函数
# ─────────────────────────────────────────────────────────────────────
def create_vector_store(
    host: str = "localhost",
    port: int = 8001,
    model: str = "BAAI/bge-small-zh-v1.5",
    dimension: int = 512,
    **kwargs,
) -> VectorStore:
    """一行创建（默认配置已适合本地开发）"""
    return VectorStore(
        host=host, port=port,
        embedding_model=model, embedding_dimension=dimension,
        **kwargs,
    )

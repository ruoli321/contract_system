# ═══════════════════════════════════════════════════════════════════
# M12 · RAG 监督学习模块
#
# RAGLearner 类：管理 contract_examples 集合（标准合同范例）
#
# 设计要点：
#   1. 范例入库：整份合同文本 → 语义切块（复用 M7 chunk_text）
#      → 每个块存入 Chroma contract_examples 集合，metadata 带 example_id + contract_type
#   2. 范例缓存：全量 example 数据（含完整文本 + 金标准提取字段）保存在内存 dict
#      这样 retrieve_examples 可以直接返回完整范例，不需要从 Chroma 反序列化
#   3. 相似检索：query Chroma 拿到 chunks → 按 example_id 去重 → 每个范例取最佳 chunk 分数
#      → 返回 top_k 唯一范例
#   4. 类型过滤：contract_type 参数 → Chroma where={"contract_type": "采购合同"} 过滤
#   5. Chroma metadata 约束：extraction dict 序列化为 JSON 字符串
#      （Chroma 只接受 str/int/float/bool，不接受嵌套 dict）
# ═══════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("rag-learner")


# ═══════════════════════════════════════════════════════════════════
# RAGLearner · 主类
# ═══════════════════════════════════════════════════════════════════

class RAGLearner:
    """
    RAG 监督学习器：标准合同范例入库 + 相似检索

    用法：
        from app.services.vector_store import VectorStore
        from app.services.rag_learner import RAGLearner

        vs = VectorStore(host="chroma", port=8000)
        learner = RAGLearner(vector_store=vs)

        # 方式 A：手动添加单个范例
        learner.add_example({
            "id": "purchase_01",
            "contract_type": "采购合同",
            "text": "完整合同文本...",
            "extraction": {"contract_name": "办公设备采购合同", "amount": 580000.0, ...},
        })

        # 方式 B：从 examples/annotations/ 批量加载标注文件（结合 PDF 解析出的文本）
        learner.load_examples("examples/annotations/", pdf_texts={"purchase_01": "..."})

        # 检索相似范例
        examples = learner.retrieve_examples("这份合同的付款方式是什么？", contract_type="采购合同", top_k=3)
    """

    def __init__(
        self,
        vector_store,
        chunker=None,
        collection_name: str = "contract_examples",
    ):
        """
        Args:
            vector_store:    M8 VectorStore 实例（Chroma 通用包装）
            chunker:         M7 chunk_text 函数或 TextChunker 实例（默认直接 import chunk_text）
            collection_name:  Chroma 目标集合名（默认 contract_examples）
        """
        self.vs = vector_store
        self.collection_name = collection_name

        # 延迟导入 chunker（避免循环依赖）
        if chunker is not None:
            self._chunk_text = chunker
        else:
            from app.services.chunker import chunk_text as _chunk
            self._chunk_text = _chunk

        # 内存缓存：example_id → {id, contract_type, text, extraction, chunk_count}
        # 每次 add_example / load_examples 时填充
        self._examples: dict[str, dict] = {}

        # ── Cross-Encoder 精排（rerank）──
        # 向量粗排（Bi-Encoder）后，对候选范例做逐对精排：query 与范例最佳块
        # 拼接送入 Cross-Encoder，token 级交互打分，排序精度高于余弦相似度。
        # 延迟加载（首次检索才初始化）+ 失败自动降级（退回纯向量排序）。
        self._reranker = None
        self._reranker_ready = False
        self._rerank_lock = threading.Lock()
        self._rerank_enabled = os.getenv("RERANK_ENABLED", "true").lower() == "true"
        self._rerank_model = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")

        logger.info(
            f"📚 RAGLearner 初始化 | collection='{collection_name}' | "
            f"vector_store={type(vector_store).__name__} | rerank={'on' if self._rerank_enabled else 'off'}"
        )

    # ═══════════════════════════════════════════════════════════════
    # 入库：单个范例
    # ═══════════════════════════════════════════════════════════════

    def add_example(self, example: dict) -> dict:
        """
        入库单个范例：切块 → Chroma + 更新内存缓存

        Args:
            example = {
                "id": "purchase_01",               # 唯一标识（必填）
                "contract_type": "采购合同",         # 合同类型（必填，用于过滤）
                "text": "...完整合同文本...",         # 合同原文（必填）
                "extraction": {                     # 金标准提取字段（可选，默认 {}）
                    "contract_name": "...",
                    "partner_a": "...",
                    "amount": 580000.0,
                    ...
                }
            }

        Returns:
            入库统计：{"example_id", "chunk_count", "collection_count"}
        """
        # ── 字段校验 ──
        required = ["id", "contract_type", "text"]
        missing = [f for f in required if not example.get(f)]
        if missing:
            raise ValueError(f"范例缺少必填字段: {missing}")

        example_id = example["id"]
        contract_type = example["contract_type"]
        text = example["text"]
        extraction = example.get("extraction", {})

        if len(text.strip()) < 50:
            logger.warning(f"  ⚠️  范例 {example_id} 文本太短（{len(text)} 字），仍尝试入库")

        # ── 1. 语义切块 ──
        t0 = time.time()
        chunks = self._chunk_text(text, source_file=example_id)
        if not chunks:
            logger.warning(f"  ⚠️  范例 {example_id} 切块失败（返回空列表），跳过入库")
            return {"example_id": example_id, "chunk_count": 0, "collection_count": 0}

        # ── 2. 构造 Chroma 数据 ──
        # Chroma metadata 值必须是 str/int/float/bool，不接受嵌套 dict
        # extraction dict 序列化为 JSON 字符串存入 metadata（主要用于调试，
        # 实际检索时从内存缓存 self._examples 拿完整数据）
        texts = [c.text for c in chunks]
        ids = [f"{example_id}:{c.index}" for c in chunks]
        metadatas = []
        for c in chunks:
            meta = {
                "example_id": example_id,
                "contract_type": contract_type,
                "chunk_index": c.index,
                "char_count": c.char_count,
                "source_file": example_id,
            }
            # clause_marker 可能为 None → Chroma 不接受 None 值 → 跳过
            if c.clause_marker:
                meta["clause_marker"] = c.clause_marker
            metadatas.append(meta)

        # ── 3. 写入 Chroma ──
        self.vs.add_texts(
            texts=texts,
            metadatas=metadatas,
            collection_name=self.collection_name,
            ids=ids,
        )

        # ── 4. 更新内存缓存 ──
        self._examples[example_id] = {
            "id": example_id,
            "contract_type": contract_type,
            "text": text,
            "extraction": extraction,
            "chunk_count": len(chunks),
        }

        elapsed = time.time() - t0
        count = self.vs.count(self.collection_name)
        logger.info(
            f"  ✅ 入库范例 {example_id} [{contract_type}] → "
            f"{len(chunks)} chunks | collection 累计 {count} 块 | 耗时 {elapsed:.2f}s"
        )

        return {
            "example_id": example_id,
            "chunk_count": len(chunks),
            "collection_count": count,
        }

    def add_examples_batch(self, examples: list[dict]) -> list[dict]:
        """批量入库（逐个调用 add_example，失败不中断）"""
        results = []
        for ex in examples:
            try:
                r = self.add_example(ex)
                results.append(r)
            except Exception as e:
                logger.error(f"  ❌ 入库范例 {ex.get('id', '?')} 失败: {e}")
                results.append({"example_id": ex.get("id", "?"), "error": str(e)})
        success = sum(1 for r in results if "error" not in r)
        logger.info(f"📥 批量入库完成: {success}/{len(examples)} 成功")
        return results

    # ═══════════════════════════════════════════════════════════════
    # 入库：从目录加载（标注 JSON + 可选文本）
    # ═══════════════════════════════════════════════════════════════

    def load_examples(
        self,
        annotations_dir: str | Path,
        texts: Optional[dict[str, str]] = None,
        texts_dir: Optional[str | Path] = None,
        reset: bool = False,
    ) -> list[dict]:
        """
        从标注目录批量加载范例。

        标注 JSON 格式（与 examples/annotations/*.json 一致）：
            {
                "id": "purchase_01",
                "contract_type": "采购合同",
                "contract_name": "办公设备采购合同",
                "partner_a": "...",
                "amount": 580000.0,
                ...
            }
        → 标注字段作为 extraction；合同原文查找链：
            1. texts dict[example_id]（调用方显式传入）
            2. annotation["text"] 字段
            3. annotation["_合同原文"] 字段（real_01 系列用这个）
            4. texts_dir/{example_id}.txt 文件（01 系列从 test_pdfs 来）

        Args:
            annotations_dir: 标注 JSON 目录路径
            texts:           可选，{example_id: "完整合同文本"} 字典
            texts_dir:       可选，存放 {id}.txt 原文文件的目录（如 test_pdfs/）
            reset:           是否先清空 contract_examples 集合再加载

        Returns:
            入库结果列表
        """
        annot_dir = Path(annotations_dir)
        if not annot_dir.exists():
            raise FileNotFoundError(f"标注目录不存在: {annot_dir}")

        if texts is None:
            texts = {}
        texts_dir_path = Path(texts_dir) if texts_dir else None

        # 重置
        if reset:
            logger.info("🗑️  reset=True → 清空 contract_examples 集合")
            self.vs.delete_collection(self.collection_name)
            self._examples.clear()

        json_files = sorted(annot_dir.glob("*.json"))
        # 过滤掉模板文件
        json_files = [f for f in json_files if not f.name.startswith("_")]

        if not json_files:
            logger.warning(f"⚠️  {annot_dir} 下没有找到标注 JSON 文件")
            return []

        logger.info(f"📂 发现 {len(json_files)} 个标注文件，准备加载...")
        if texts_dir_path and texts_dir_path.exists():
            logger.info(f"📄 自动扫描原文目录: {texts_dir_path}")

        examples = []
        skipped = 0
        for jf in json_files:
            try:
                annotation = json.loads(jf.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"  ⚠️  跳过 {jf.name}：JSON 解析失败 - {e}")
                skipped += 1
                continue

            example_id = annotation.get("id")
            contract_type = annotation.get("contract_type")
            if not example_id or not contract_type:
                logger.warning(f"  ⚠️  跳过 {jf.name}：缺少 id 或 contract_type")
                skipped += 1
                continue

            # 合同原文查找链（按优先级）：
            # 1. 显式传入的 texts dict
            # 2. annotation["text"] 字段
            # 3. annotation["_合同原文"] 字段（real_01 系列用这个）
            # 4. texts_dir/{example_id}.txt 文件
            text = texts.get(example_id) or annotation.get("text") or annotation.get("_合同原文")
            if (not text or len(text.strip()) < 50) and texts_dir_path:
                txt_file = texts_dir_path / f"{example_id}.txt"
                if txt_file.exists():
                    text = txt_file.read_text(encoding="utf-8")
                    logger.debug(f"    ↳ 从 {txt_file.name} 读取原文 ({len(text)} 字)")

            if not text or len(text.strip()) < 50:
                logger.warning(
                    f"  ⚠️  跳过 {jf.name}：缺少合同原文"
                )
                skipped += 1
                continue

            # 标注里的业务字段就是金标准 extraction
            # 去掉 "id"、"contract_type"、以及下划线开头的内部字段（_合同原文 等）
            extraction = {
                k: v for k, v in annotation.items()
                if not k.startswith("_") and k not in ("id", "contract_type", "text")
            }

            examples.append({
                "id": example_id,
                "contract_type": contract_type,
                "text": text,
                "extraction": extraction,
            })

        logger.info(f"  ✅ 有效范例 {len(examples)} 份，跳过 {skipped} 份")
        return self.add_examples_batch(examples)

    # ═══════════════════════════════════════════════════════════════
    # 检索：相似范例
    # ═══════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════
    # Cross-Encoder 精排（rerank）
    # ═══════════════════════════════════════════════════════════════

    def _rerank_scores(self, query_text: str, texts: list[str]) -> Optional[list[float]]:
        """
        Cross-Encoder 精排：对 (query, text) 逐对打分。

        Returns:
            分数列表（与 texts 等长）；未启用/加载失败返回 None（调用方降级为向量分）。
        """
        if not self._rerank_enabled:
            return None
        with self._rerank_lock:
            if not self._reranker_ready:
                try:
                    from sentence_transformers import CrossEncoder
                    logger.info(f"  ⏳ 加载 rerank 模型: {self._rerank_model}")
                    self._reranker = CrossEncoder(self._rerank_model, max_length=512)
                    self._reranker_ready = True
                    logger.info("  ✅ rerank 模型就绪")
                except Exception as e:
                    logger.warning(f"  ⚠️ rerank 模型加载失败，降级为纯向量排序: {e}")
                    self._rerank_enabled = False  # 本次进程内不再重试
                    return None
        try:
            pairs = [(query_text, t) for t in texts]
            scores = self._reranker.predict(pairs)
            return [float(s) for s in scores]
        except Exception as e:
            logger.warning(f"  ⚠️ rerank 推理失败，降级为纯向量排序: {e}")
            return None

    def retrieve_examples(
        self,
        query_text: str,
        contract_type: Optional[str] = None,
        top_k: int = 3,
        exclude_ids: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        根据查询文本检索最相似的标准合同范例。

        检索策略：
        1. 在 contract_examples 集合中搜索与 query_text 最相似的 chunks
        2. 按 example_id 去重 —— 同一个范例的多个 chunk 都命中时，取最高相似度
        3. 返回 top_k 个唯一范例（含完整文本 + 金标准提取字段 + 最佳匹配分数）

        Args:
            query_text:      查询文本（可以是合同片段、问题描述、字段值等）
            contract_type:   可选，按合同类型过滤（Chroma where 精确匹配）
            top_k:           返回范例数量（唯一范例，不是 chunks）
            exclude_ids:     可选，要排除的 example_id 列表（leave-one-out 防数据泄漏）

        Returns:
            [
                {
                    "id": "purchase_01",
                    "contract_type": "采购合同",
                    "text": "完整合同文本...",
                    "extraction": {"contract_name": "...", "amount": 580000.0, ...},
                    "score": 0.8523,           # 该范例最佳匹配 chunk 的相似度
                    "matched_chunk": "xxx...",  # 最佳匹配 chunk 的文本片段
                    "chunk_count": 12,
                },
                ...
            ]
        """
        if not query_text or not query_text.strip():
            logger.warning("⚠️  retrieve_examples: query_text 为空")
            return []

        # ── 1. 构造过滤条件 ──
        where_filter = None
        if contract_type:
            where_filter = {"contract_type": contract_type}

        # 检索更多 chunks 以确保能聚合出足够的唯一范例
        # （每个范例平均 10-15 个 chunks，所以查 top_k * 20 个 chunks 基本够）
        chunk_k = max(top_k * 20, 10)

        # ── 2. 向量检索 ──
        try:
            chunk_results = self.vs.query(
                query_text=query_text,
                collection_name=self.collection_name,
                top_k=chunk_k,
                filter=where_filter,
            )
        except Exception as e:
            logger.error(f"❌ 范例检索失败: {e}")
            return []

        if not chunk_results:
            logger.info(f"  🔍 集合 '{self.collection_name}' 为空，无匹配范例")
            return []

        # ── 3. 按 example_id 聚合（取每个范例的最佳匹配分数）──
        example_best: dict[str, dict] = {}
        for chunk in chunk_results:
            meta = chunk.get("metadata", {})
            eid = meta.get("example_id")
            if not eid:
                continue

            similarity = chunk.get("similarity", 0.0)
            if eid not in example_best or similarity > example_best[eid]["score"]:
                example_best[eid] = {
                    "score": similarity,
                    "matched_chunk": chunk.get("text", ""),
                }

        # ── 排除指定的 example_id（leave-one-out 防数据泄漏）──
        if exclude_ids:
            example_best = {
                eid: v for eid, v in example_best.items()
                if eid not in exclude_ids
            }

        # ── 4. Cross-Encoder 精排（rerank）→ 按精排分排序，取 top_k ──
        # 粗排分仅用于召回；最终排序以 Cross-Encoder 逐对打分为准。
        # rerank 不可用（未启用/加载失败/推理失败）时降级回向量相似度排序。
        cand_eids = list(example_best.keys())
        rerank_scores = self._rerank_scores(
            query_text, [example_best[eid]["matched_chunk"] for eid in cand_eids]
        )
        if rerank_scores is not None:
            for eid, s in zip(cand_eids, rerank_scores):
                example_best[eid]["rerank_score"] = s
            sort_key = lambda eid: example_best[eid]["rerank_score"]  # noqa: E731
        else:
            sort_key = lambda eid: example_best[eid]["score"]  # noqa: E731
        sorted_ids = sorted(cand_eids, key=sort_key, reverse=True)[:top_k]

        # ── 5. 组装完整范例数据 ──
        results = []
        for eid in sorted_ids:
            best = example_best[eid]
            cached = self._examples.get(eid)
            if cached:
                # 内存缓存有完整数据（理想情况）
                results.append({
                    "id": cached["id"],
                    "contract_type": cached["contract_type"],
                    "text": cached["text"],
                    "extraction": cached["extraction"],
                    "score": round(best["score"], 4),
                    "matched_chunk": best["matched_chunk"],
                    "chunk_count": cached["chunk_count"],
                })
            else:
                # 内存缓存没有（可能是重启后 Chroma 还在但内存丢了）
                # 降级：只返回 Chroma metadata 里有的字段
                results.append({
                    "id": eid,
                    "contract_type": None,
                    "text": "",
                    "extraction": {},
                    "score": round(best["score"], 4),
                    "matched_chunk": best["matched_chunk"],
                    "chunk_count": 0,
                    "_note": "内存缓存未命中，可能需要重新 load_examples()",
                })

        logger.info(
            f"🔎 检索范例: query='{query_text[:30]}...' → {len(results)} 个范例"
            + (f" | 类型过滤: {contract_type}" if contract_type else "")
        )
        if results:
            top = results[0]
            logger.info(f"  🥇 Top1: {top['id']} [{top['contract_type']}] score={top['score']}")

        return results

    # ═══════════════════════════════════════════════════════════════
    # Few-shot 格式化：把范例列表拼成 LLM prompt 能直接引用的文本块
    # ═══════════════════════════════════════════════════════════════

    def format_few_shot(self, examples: list[dict], max_text_len: int = 500) -> str:
        """
        把 retrieve_examples() 返回的范例列表格式化为 few-shot 示例文本。

        输出格式：
            【范例 1 · 采购合同】（相似度 0.85）
            合同片段：甲方北京科技有限公司向乙方上海贸易有限公司采购服务器...
            正确提取结果：{"contract_name": "办公设备采购合同", "amount": 500000.0, ...}

            【范例 2 · 销售合同】...

        Args:
            examples:      retrieve_examples() 返回的范例列表
            max_text_len:  每个范例的合同片段最大字符数（截断）

        Returns:
            格式化后的字符串，可直接拼进 user_prompt_template 的 {few_shot_examples} 占位符
            无范例时返回 "（暂无参考范例）"
        """
        if not examples:
            return "（暂无参考范例）"

        blocks = []
        for i, ex in enumerate(examples):
            # 合同文本（截断）
            text = ex.get("text", "")
            if len(text) > max_text_len:
                text = text[:max_text_len] + "..."

            # 金标准提取字段（JSON）
            extraction = ex.get("extraction", {})
            extraction_str = json.dumps(extraction, ensure_ascii=False, indent=2)

            ctype = ex.get("contract_type") or "未知"
            score = ex.get("score")
            score_label = f"（相似度 {score:.2f}）" if score is not None else ""

            block = (
                f"【范例 {i + 1} · {ctype}】{score_label}\n"
                f"合同片段：{text}\n"
                f"正确提取结果：{extraction_str}"
            )
            blocks.append(block)

        result = "\n\n".join(blocks)
        logger.debug(f"📝 format_few_shot: {len(examples)} 个范例 → {len(result)} 字符")
        return result

    # ── 便捷方法：检索 + 格式化一步到位 ──
    def retrieve_and_format_few_shot(
        self,
        query_text: str,
        contract_type: Optional[str] = None,
        top_k: int = 3,
        max_text_len: int = 500,
        exclude_ids: Optional[list[str]] = None,
    ) -> tuple[str, list[dict]]:
        """
        检索相似范例并格式化为 few-shot。

        Args:
            exclude_ids: 可选，要排除的 example_id 列表（leave-one-out 防数据泄漏）

        Returns:
            (few_shot_str, raw_examples) — few_shot_str 可直接拼进 prompt，
            raw_examples 含完整范例元信息（用于日志记录）
        """
        examples = self.retrieve_examples(
            query_text, contract_type=contract_type, top_k=top_k, exclude_ids=exclude_ids
        )
        few_shot_str = self.format_few_shot(examples, max_text_len=max_text_len)
        return few_shot_str, examples

    # ═══════════════════════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════════════════════

    def get_example(self, example_id: str) -> Optional[dict]:
        """从内存缓存获取单个范例"""
        return self._examples.get(example_id)

    def list_examples(self, contract_type: Optional[str] = None) -> list[dict]:
        """列出已加载的所有范例（从内存缓存）"""
        examples = list(self._examples.values())
        if contract_type:
            examples = [e for e in examples if e["contract_type"] == contract_type]
        return examples

    def stats(self) -> dict:
        """统计信息"""
        by_type: dict[str, int] = {}
        total_chunks = 0
        for ex in self._examples.values():
            ct = ex["contract_type"]
            by_type[ct] = by_type.get(ct, 0) + 1
            total_chunks += ex["chunk_count"]

        chroma_count = 0
        try:
            chroma_count = self.vs.count(self.collection_name)
        except Exception:
            pass

        return {
            "total_examples": len(self._examples),
            "total_chunks_in_memory": total_chunks,
            "total_chunks_in_chroma": chroma_count,
            "by_contract_type": by_type,
            "collection_name": self.collection_name,
        }

    def clear_memory(self):
        """清空内存缓存（Chroma 数据不动）"""
        self._examples.clear()
        logger.info("🧹 RAGLearner 内存缓存已清空")

    def reset_collection(self):
        """清空 Chroma 集合 + 内存缓存"""
        self.vs.delete_collection(self.collection_name)
        self._examples.clear()
        logger.info(f"💥 集合 '{self.collection_name}' 已重置")

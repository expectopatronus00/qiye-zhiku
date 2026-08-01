"""检索模块 - 支持向量检索 + BM25 混合检索"""
from typing import Optional
from app.core.config import settings
from app.core.vectorstore import VectorStore
from app.core.embeddings import EmbeddingService


class Retriever:
    """混合检索器"""

    def __init__(self, collection_name: str = "default"):
        self.vectorstore = VectorStore(collection_name=collection_name)
        self.embedding_service = EmbeddingService()
        self.top_k = settings.retrieval.top_k
        self.hybrid = settings.retrieval.hybrid_search
        self.bm25_weight = settings.retrieval.bm25_weight

    async def retrieve(self, query: str) -> list[dict]:
        """检索相关文档"""
        # 生成查询向量
        query_embedding = await self.embedding_service.embed_query(query)

        # 向量检索
        vector_results = self.vectorstore.search(
            query_embedding=query_embedding,
            top_k=self.top_k,
        )

        if not self.hybrid:
            return vector_results

        # 混合检索：结合 BM25
        try:
            bm25_results = self._bm25_search(query, top_k=self.top_k)
            if bm25_results:
                return self._merge_results(vector_results, bm25_results)
        except Exception:
            # BM25 失败时退回纯向量检索
            pass

        return vector_results

    def _bm25_search(self, query: str, top_k: int = 5) -> list[dict]:
        """BM25 关键词检索"""
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            return []

        # 获取所有文档
        all_docs = self.vectorstore.collection.get(
            include=["documents", "metadatas"]
        )

        if not all_docs["documents"]:
            return []

        # 分词（简单按字符分割，中文需要改进）
        tokenized_corpus = [doc.split() for doc in all_docs["documents"]]
        tokenized_query = query.split()

        bm25 = BM25Okapi(tokenized_corpus)
        scores = bm25.get_scores(tokenized_query)

        # 获取 top_k 结果
        top_indices = sorted(
            range(len(scores)),
            key=lambda i: scores[i],
            reverse=True,
        )[:top_k]

        results = []
        for idx in top_indices:
            if scores[idx] > 0:
                results.append({
                    "content": all_docs["documents"][idx],
                    "metadata": all_docs["metadatas"][idx],
                    "score": float(scores[idx]),
                    "source": "bm25",
                })
        return results

    def _merge_results(
        self,
        vector_results: list[dict],
        bm25_results: list[dict],
    ) -> list[dict]:
        """合并向量和 BM25 结果（RRF 融合）"""
        vector_weight = 1 - self.bm25_weight
        merged = {}

        # 向量结果
        for rank, doc in enumerate(vector_results):
            key = doc["content"][:100]  # 用内容前100字符作为去重键
            rrf_score = vector_weight / (rank + 1)
            merged[key] = {**doc, "rrf_score": rrf_score, "source": "hybrid"}

        # BM25 结果
        for rank, doc in enumerate(bm25_results):
            key = doc["content"][:100]
            rrf_score = self.bm25_weight / (rank + 1)
            if key in merged:
                merged[key]["rrf_score"] += rrf_score
                merged[key]["source"] = "hybrid"
            else:
                merged[key] = {**doc, "rrf_score": rrf_score, "source": "hybrid"}

        # 按 RRF 分数排序
        sorted_results = sorted(
            merged.values(),
            key=lambda x: x["rrf_score"],
            reverse=True,
        )
        return sorted_results[:self.top_k]

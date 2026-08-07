"""检索模块 - 支持向量检索 + BM25 混合检索

Day 3 优化：
- BM25 中文分词（jieba），解决按空格切词导致中文检索失效的问题
- 应用 score_threshold 相似度阈值过滤低质量结果
- 向量检索支持按阈值过滤

Day 4 优化：
- 集成 Reranker 精排（cross-encoder / 启发式降级）

v1.2 优化（检索效果工程）：
- 标准 RRF 融合：score = w_v/(k+rank_v) + w_b/(k+rank_b)，k=60，
  替代原简化 rank 加权（原式对单路命中的分数衰减过快）
- 检索诊断数据：last_debug 记录每条结果的路径排名/得分/融合分/耗时，
  供前端"检索详情"面板与评测回归使用
"""
import time
from typing import Optional
from app.core.config import settings
from app.core.vectorstore import VectorStore
from app.core.embeddings import EmbeddingService
from app.core.reranker import reranker

_RRF_K = 60  # 标准 RRF 常数


def _tokenize(text: str) -> list[str]:
    """中文分词：优先 jieba，降级为按空格+单字切分"""
    try:
        import jieba
        return [w.strip() for w in jieba.cut(text) if w.strip()]
    except ImportError:
        # 降级方案：空格分词 + 中文按单字切分
        tokens = text.split()
        tokens += [ch for ch in text if "\u4e00" <= ch <= "\u9fff"]
        return tokens


class Retriever:
    """混合检索器"""

    def __init__(self, collection_name: str = "default"):
        self.vectorstore = VectorStore(collection_name=collection_name)
        self.embedding_service = EmbeddingService()
        self.top_k = settings.retrieval.top_k
        self.hybrid = settings.retrieval.hybrid_search
        self.bm25_weight = settings.retrieval.bm25_weight
        self.score_threshold = settings.retrieval.score_threshold
        self.last_debug: Optional[dict] = None  # 最近一次检索的诊断数据（v1.2）

    async def retrieve(self, query: str) -> list[dict]:
        """检索相关文档（每次调用刷新 last_debug 诊断数据）"""
        start = time.perf_counter()
        query_embedding = await self.embedding_service.embed_query(query)

        # 向量检索
        vector_results = self.vectorstore.search(
            query_embedding=query_embedding,
            top_k=self.top_k,
        )

        # 阈值过滤（向量相似度低于阈值的结果丢弃）
        vector_results = self._filter_by_threshold(vector_results)

        if not self.hybrid:
            reranked = reranker.rerank(query, vector_results)
            self.last_debug = {
                "query": query,
                "hybrid": False,
                "fusion": "vector-only",
                "bm25_weight": 0.0,
                "vector_hits": len(vector_results),
                "bm25_hits": 0,
                "vector_top_score": round(vector_results[0].get("score", 0), 4) if vector_results else 0,
                "bm25_top_score": 0,
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
                "paths": [
                    {"content_prefix": d["content"][:60], "vector_rank": i + 1,
                     "vector_score": round(d.get("score", 0), 4),
                     "bm25_rank": None, "bm25_score": None, "rrf": None}
                    for i, d in enumerate(vector_results)
                ],
            }
            return reranked

        # 混合检索：结合 BM25
        try:
            bm25_results = self._bm25_search(query, top_k=self.top_k)
            if bm25_results:
                merged, paths = self._merge_results(vector_results, bm25_results)
                self.last_debug = {
                    "query": query,
                    "hybrid": True,
                    "fusion": f"rrf(k={_RRF_K})",
                    "bm25_weight": self.bm25_weight,
                    "vector_hits": len(vector_results),
                    "bm25_hits": len(bm25_results),
                    "vector_top_score": round(vector_results[0].get("score", 0), 4) if vector_results else 0,
                    "bm25_top_score": round(bm25_results[0].get("score", 0), 4) if bm25_results else 0,
                    "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
                    "paths": paths,
                }
                return reranker.rerank(query, merged)
        except Exception:
            # BM25 失败时退回纯向量检索
            pass

        reranked = reranker.rerank(query, vector_results)
        self.last_debug = {
            "query": query,
            "hybrid": True,
            "fusion": "vector-only(bm25_fallback)",
            "bm25_weight": self.bm25_weight,
            "vector_hits": len(vector_results),
            "bm25_hits": 0,
            "vector_top_score": round(vector_results[0].get("score", 0), 4) if vector_results else 0,
            "bm25_top_score": 0,
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
            "paths": [
                {"content_prefix": d["content"][:60], "vector_rank": i + 1,
                 "vector_score": round(d.get("score", 0), 4),
                 "bm25_rank": None, "bm25_score": None, "rrf": None}
                for i, d in enumerate(vector_results)
            ],
        }
        return reranked

    def _filter_by_threshold(self, results: list[dict]) -> list[dict]:
        """按相似度阈值过滤（vectorstore 的 score 已是 1-distance 相似度）"""
        if self.score_threshold <= 0 or not results:
            return results

        filtered = [doc for doc in results if doc.get("score", 0) >= self.score_threshold]
        return filtered or results  # 全被过滤时保留原结果，避免空检索

    def _bm25_search(self, query: str, top_k: int = 5) -> list[dict]:
        """BM25 关键词检索（中文分词）"""
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

        # 中文分词
        tokenized_corpus = [_tokenize(doc) for doc in all_docs["documents"]]
        tokenized_query = _tokenize(query)

        # 过滤空文档
        valid_pairs = [
            (tok, meta, content)
            for tok, meta, content in zip(
                tokenized_corpus,
                all_docs["metadatas"],
                all_docs["documents"],
            )
            if tok
        ]
        if not valid_pairs:
            return []

        bm25 = BM25Okapi([t for t, _, _ in valid_pairs])
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
                    "content": valid_pairs[idx][2],
                    "metadata": valid_pairs[idx][1],
                    "score": float(scores[idx]),
                    "source": "bm25",
                })
        return results

    def _merge_results(
        self,
        vector_results: list[dict],
        bm25_results: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """标准 RRF 融合向量与 BM25 结果，返回 (结果, 路径诊断)

        RRF 公式：score(d) = w_v / (k + rank_v(d)) + w_b / (k + rank_b(d))
        - k = 60（标准常数）
        - w_v = 1 - bm25_weight，w_b = bm25_weight（保留配置可调）
        - 单路命中时另一路贡献为 0
        去重键取内容前 100 字符（与旧版一致）。
        """
        vector_weight = 1 - self.bm25_weight
        merged: dict[str, dict] = {}
        path_rank: dict[str, dict] = {}

        # 向量结果
        for rank, doc in enumerate(vector_results):
            key = doc["content"][:100]  # 用内容前100字符作为去重键
            rrf = vector_weight / (_RRF_K + rank + 1)
            merged[key] = {**doc, "rrf_score": rrf, "source": "hybrid"}
            path_rank[key] = {
                "content_prefix": doc["content"][:60],
                "vector_rank": rank + 1,
                "vector_score": round(doc.get("score", 0), 4),
                "bm25_rank": None,
                "bm25_score": None,
                "rrf": round(rrf, 6),
            }

        # BM25 结果
        for rank, doc in enumerate(bm25_results):
            key = doc["content"][:100]
            rrf = self.bm25_weight / (_RRF_K + rank + 1)
            if key in merged:
                merged[key]["rrf_score"] += rrf
                merged[key]["source"] = "hybrid"
                path_rank[key]["bm25_rank"] = rank + 1
                path_rank[key]["bm25_score"] = round(doc.get("score", 0), 4)
                path_rank[key]["rrf"] = round(merged[key]["rrf_score"], 6)
            else:
                merged[key] = {**doc, "rrf_score": rrf, "source": "hybrid"}
                path_rank[key] = {
                    "content_prefix": doc["content"][:60],
                    "vector_rank": None,
                    "vector_score": None,
                    "bm25_rank": rank + 1,
                    "bm25_score": round(doc.get("score", 0), 4),
                    "rrf": round(rrf, 6),
                }

        # 按 RRF 分数排序
        sorted_results = sorted(
            merged.values(),
            key=lambda x: x["rrf_score"],
            reverse=True,
        )
        return sorted_results[:self.top_k], list(path_rank.values())

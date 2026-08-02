"""重排序模块 - 检索结果精排

Day 4 特性：
- cross_encoder 模式：本地加载 bge-reranker-base 等交叉编码器模型，对 (query, doc) 对打分
- heuristic 模式：零依赖启发式精排（查询词覆盖率 + 原始分数 + 位置加权）
- 模型缺失/加载失败时自动降级为 heuristic，保证服务可用性

重排流程：retriever 召回 top_k 候选 → reranker 打分 → 按新分数排序截断 top_n
"""
import logging
import threading
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


def _sigmoid(x: float) -> float:
    """sigmoid 归一化（bge-reranker 输出 logits，sigmoid 后映射到 0-1）"""
    import math
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


class RerankerService:
    """检索结果重排序服务"""

    def __init__(self, enabled: bool = True, rtype: str = "cross_encoder",
                 model_path: str = "", top_n: int = 5):
        self.enabled = enabled
        self.rtype = rtype if rtype in ("cross_encoder", "heuristic", "none") else "heuristic"
        self.model_path = model_path
        self.top_n = max(1, min(top_n, 10))
        self._model = None
        self._model_loaded = False
        self._load_lock = threading.Lock()

    # ========== 模型加载 ==========

    def _load_model(self):
        """懒加载 cross-encoder 模型，失败自动降级为 heuristic"""
        if self._model_loaded:
            return
        with self._load_lock:
            if self._model_loaded:
                return
            if self.rtype != "cross_encoder":
                self._model_loaded = True
                return
            try:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(self.model_path, max_length=512)
                logger.info("Reranker model loaded from %s", self.model_path)
            except Exception as e:
                logger.warning(
                    "Reranker model load failed (%s), fallback to heuristic", e
                )
                self.rtype = "heuristic"
            finally:
                self._model_loaded = True

    # ========== 主入口 ==========

    def rerank(self, query: str, docs: list[dict], top_n: Optional[int] = None) -> list[dict]:
        """对候选文档重排序

        Args:
            query: 原始查询（建议用改写后的完整查询）
            docs: 检索候选文档列表（含 content / score / metadata）
            top_n: 返回条数，默认使用配置值

        Returns:
            重排后的文档列表，每条含 rerank_score（最终分数）与 original_score
        """
        if not self.enabled or self.rtype == "none" or not docs:
            return docs

        n = top_n or self.top_n
        self._load_model()

        if self.rtype == "cross_encoder" and self._model is not None:
            ranked = self._cross_encoder_rerank(query, docs)
        else:
            ranked = self._heuristic_rerank(query, docs)

        return ranked[:n]

    # ========== cross-encoder 精排 ==========

    def _cross_encoder_rerank(self, query: str, docs: list[dict]) -> list[dict]:
        """用交叉编码器对 (query, doc) 逐对打分"""
        pairs = [(query, d.get("content", "")) for d in docs]
        try:
            logits = self._model.predict(pairs)
        except Exception as e:
            logger.warning("CrossEncoder predict failed (%s), fallback to heuristic", e)
            return self._heuristic_rerank(query, docs)

        results = []
        for doc, logit in zip(docs, logits):
            score = _sigmoid(float(logit))
            results.append({
                **doc,
                "original_score": doc.get("score", 0.0),
                "rerank_score": round(score, 4),
                "score": round(score, 4),
                "reranker": "cross_encoder",
            })
        results.sort(key=lambda x: x["rerank_score"], reverse=True)
        return results

    # ========== 启发式精排（零依赖降级方案） ==========

    def _heuristic_rerank(self, query: str, docs: list[dict]) -> list[dict]:
        """启发式精排：查询词覆盖率 + 原始分数 + 位置加权"""
        query_tokens = self._tokenize(query)
        total = len(docs)

        results = []
        for rank, doc in enumerate(docs):
            content = doc.get("content", "")
            content_tokens = set(self._tokenize(content))

            # 1. 查询词覆盖率（0-1）：查询词中有多少出现在文档中
            hits = sum(1 for t in query_tokens if t in content_tokens) if query_tokens else 0
            coverage = hits / len(query_tokens) if query_tokens else 0.0

            # 2. 原始分数（RRF 分数不是 0-1，先归一化）
            raw = doc.get("score", 0.0)
            max_raw = max((d.get("score", 0.0) for d in docs), default=1.0) or 1.0
            norm_raw = max(0.0, min(1.0, raw / max_raw))

            # 3. 位置加权：召回排名越靠前越可信
            position = 1.0 - (rank / max(total, 1))

            # 加权融合：覆盖率 50% + 原始分数 35% + 位置 15%
            final = 0.5 * coverage + 0.35 * norm_raw + 0.15 * position

            results.append({
                **doc,
                "original_score": doc.get("score", 0.0),
                "rerank_score": round(final, 4),
                "score": round(final, 4),
                "reranker": "heuristic",
            })

        results.sort(key=lambda x: x["rerank_score"], reverse=True)
        return results

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中文分词（与 retriever 保持一致）"""
        try:
            import jieba
            tokens = [w.strip() for w in jieba.cut(text) if w.strip()]
        except ImportError:
            tokens = [ch for ch in text if "\u4e00" <= ch <= "\u9fff"]
        # 过滤单字噪声（启发式打分中常见停用字）
        return [t for t in tokens if len(t) > 1 or t.isascii() and len(t) >= 2]


# 全局实例（由配置驱动）
_cfg = settings.reranker
reranker = RerankerService(
    enabled=_cfg.enabled,
    rtype=_cfg.type,
    model_path=_cfg.model_path,
    top_n=_cfg.top_n,
)

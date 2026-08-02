"""Reranker 单元测试 - Day 4"""
import pytest

from app.core.reranker import RerankerService, _sigmoid


@pytest.fixture
def docs():
    """模拟混合检索候选结果（RRF 分数非归一化）"""
    return [
        {"content": "GPU热点预测是基于GPU运行数据分析热区的技术，可提前调度散热资源，实现节能30%以上",
         "metadata": {"filename": "a.md"}, "score": 0.8, "source": "hybrid"},
        {"content": "本文介绍数据中心基础设施的总体架构与部署方案，包括机柜、供电与制冷系统",
         "metadata": {"filename": "b.md"}, "score": 0.6, "source": "hybrid"},
        {"content": "GPU热点预测需要采集温度、利用率和功耗等指标，通过模型预测未来可能出现的热区",
         "metadata": {"filename": "c.md"}, "score": 0.75, "source": "hybrid"},
        {"content": "企业数字化转型路线图，涵盖智能制造、智慧能源等央企AI高价值场景",
         "metadata": {"filename": "d.md"}, "score": 0.55, "source": "hybrid"},
    ]


class TestSigmoid:
    def test_sigmoid_range(self):
        assert 0.0 < _sigmoid(0) < 1.0
        assert _sigmoid(10) > 0.99
        assert _sigmoid(-10) < 0.01

    def test_sigmoid_extreme_no_overflow(self):
        assert _sigmoid(1000) == 1.0
        assert _sigmoid(-1000) == 0.0


class TestHeuristicRerank:
    def test_disabled_returns_original(self):
        svc = RerankerService(enabled=False)
        docs = [{"content": "a", "score": 0.5}]
        assert svc.rerank("查询", docs) == docs

    def test_none_type_returns_original(self):
        svc = RerankerService(enabled=True, rtype="none")
        docs = [{"content": "a", "score": 0.5}]
        assert svc.rerank("查询", docs) == docs

    def test_empty_docs(self):
        svc = RerankerService(enabled=True)
        assert svc.rerank("查询", []) == []

    def test_relevant_docs_ranked_first(self, docs):
        """查询词覆盖率高的文档应排到前面"""
        svc = RerankerService(enabled=True, rtype="heuristic")
        ranked = svc.rerank("GPU热点预测的作用是什么", docs)

        # 相关文档排前，无关文档排后
        assert ranked[0]["content"].startswith("GPU热点预测是基于")
        assert ranked[0]["reranker"] == "heuristic"
        # 所有结果带 rerank_score 且 0-1 范围内
        for d in ranked:
            assert 0.0 <= d["rerank_score"] <= 1.0
            assert "original_score" in d

    def test_top_n_truncation(self, docs):
        svc = RerankerService(enabled=True, rtype="heuristic", top_n=2)
        ranked = svc.rerank("GPU热点预测", docs)
        assert len(ranked) == 2

    def test_heuristic_is_stable(self, docs):
        """同输入多次重排结果一致（无随机性）"""
        svc = RerankerService(enabled=True, rtype="heuristic")
        r1 = svc.rerank("GPU热点预测", docs)
        r2 = svc.rerank("GPU热点预测", docs)
        assert [d["content"] for d in r1] == [d["content"] for d in r2]


class TestCrossEncoderFallback:
    def test_missing_model_falls_back_to_heuristic(self, docs):
        """模型路径不存在时自动降级 heuristic，不抛异常"""
        svc = RerankerService(
            enabled=True,
            rtype="cross_encoder",
            model_path="C:/nonexistent/reranker-model",
            top_n=5,
        )
        ranked = svc.rerank("GPU热点预测", docs)
        assert ranked
        assert all(d["reranker"] == "heuristic" for d in ranked)
        assert ranked[0]["content"].startswith("GPU热点预测是基于")

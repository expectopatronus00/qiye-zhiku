"""v1.2 检索效果工程单元测试 - RRF 融合 / 反馈闭环 / 评测回归工具"""
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.retriever import Retriever, _RRF_K
from app.core.security import (
    _DB,
    FeedbackManager,
)
from main import app


@pytest.fixture()
def db(tmp_path) -> _DB:
    return _DB(str(tmp_path / "test_feedback.db"))


@pytest.fixture()
def fb(db: _DB) -> FeedbackManager:
    return FeedbackManager(db)


# ---------------- RRF 融合（核心层） ----------------

class TestRrfMerge:
    def _v(self, content: str, score: float = 0.8):
        return {"content": content, "metadata": {"filename": "a.pdf"},
                "score": score, "source": "vector"}

    def _b(self, content: str, score: float = 5.0):
        return {"content": content, "metadata": {"filename": "b.pdf"},
                "score": score, "source": "bm25"}

    def _make_retriever(self) -> Retriever:
        r = Retriever.__new__(Retriever)  # 跳过 __init__（不连真实向量库）
        r.bm25_weight = 0.3
        r.top_k = 10
        return r

    def test_double_hit_scores_above_single_hit(self):
        """双路命中（向量+BM25 同时召回）融合分应高于仅单路命中的文档"""
        r = self._make_retriever()
        merged, paths = r._merge_results(
            [self._v("AAAA 文档内容", 0.9), self._v("BBBB 仅向量命中", 0.7)],
            [self._b("BBBB 仅向量命中", 6.0), self._b("AAAA 文档内容", 3.0)],
        )
        by_content = {d["content"][:100]: d for d in merged}
        double = by_content["AAAA 文档内容"]
        single = by_content["BBBB 仅向量命中"]
        assert double["rrf_score"] > single["rrf_score"]
        # 双路 = w_v/(k+1) + w_b/(k+2)  单路 = w_v/(k+2) + w_b/(k+1)
        wv, wb = 0.7, 0.3
        expect_double = wv / (_RRF_K + 1) + wb / (_RRF_K + 2)
        expect_single = wv / (_RRF_K + 2) + wb / (_RRF_K + 1)
        assert abs(double["rrf_score"] - expect_double) < 1e-9
        assert abs(single["rrf_score"] - expect_single) < 1e-9

    def test_single_path_contribution(self):
        """仅 BM25 命中的文档应获得 w_b/(k+rank) 且 vector 字段为空"""
        r = self._make_retriever()
        merged, paths = r._merge_results(
            [self._v("仅向量命中文档", 0.8)],
            [self._b("仅BM25命中文档", 4.0)],
        )
        assert len(merged) == 2
        bm25_only = next(d for d in merged if "BM25命中" in d["content"])
        assert abs(bm25_only["rrf_score"] - 0.3 / (_RRF_K + 1)) < 1e-9
        p = next(p for p in paths if p["content_prefix"].startswith("仅BM25"))
        assert p["vector_rank"] is None and p["bm25_rank"] == 1

    def test_weight_balance(self):
        """bm25_weight=0 时 BM25 完全不贡献（等价纯向量排序）"""
        r = self._make_retriever()
        r.bm25_weight = 0.0
        merged, _ = r._merge_results(
            [self._v("AAA", 0.9), self._v("BBB", 0.8)],
            [self._b("ZZZ 高BM25分", 99.0)],
        )
        # 权重为 0 时 BM25 结果仍出现但分数为 0，排序应按向量
        assert merged[0]["content"].startswith("AAA")
        assert merged[-1]["rrf_score"] == 0.0

    def test_dedup_by_content_prefix(self):
        """同一内容双路召回应去重为一条（hybrid source）"""
        r = self._make_retriever()
        merged, _ = r._merge_results(
            [self._v("相同内容片段 abc"), self._v("不同内容 123")],
            [self._b("相同内容片段 abc")],
        )
        assert len(merged) == 2
        dup = next(d for d in merged if "相同内容" in d["content"])
        assert dup["source"] == "hybrid"


# ---------------- 反馈管理（核心层） ----------------

class TestFeedbackCore:
    def test_add_and_list(self, fb: FeedbackManager):
        fid = fb.add("m1", "alice", "up", question="问题A", answer="回答A",
                     conversation_id="c1", collection_name="default")
        assert fid > 0
        items = fb.list()
        assert len(items) == 1
        assert items[0]["rating"] == "up" and items[0]["question"] == "问题A"

    def test_resubmit_overwrites(self, fb: FeedbackManager):
        fb.add("m1", "alice", "up", question="Q")
        fb.add("m1", "alice", "down", reason="不准确", expected_answer="期望答案")
        items = fb.list()
        assert len(items) == 1  # 同一消息只保留最新
        assert items[0]["rating"] == "down"
        assert items[0]["expected_answer"] == "期望答案"

    def test_invalid_rating_rejected(self, fb: FeedbackManager):
        with pytest.raises(ValueError):
            fb.add("m1", "alice", "meh")

    def test_export_dataset_only_down_with_expected(self, fb: FeedbackManager):
        fb.add("m1", "alice", "up", question="好的问题")
        fb.add("m2", "bob", "down", question="坏的问题", expected_answer="正确回答")
        fb.add("m3", "bob", "down", question="无期望回答的问题")
        data = fb.export_dataset()
        assert len(data["items"]) == 1
        assert data["items"][0]["golden_answer"] == "正确回答"
        assert data["items"][0]["origin"] == "user_feedback"

    def test_list_filter_by_rating(self, fb: FeedbackManager):
        fb.add("m1", "alice", "up")
        fb.add("m2", "bob", "down")
        ups = fb.list(rating="up")
        downs = fb.list(rating="down")
        assert len(ups) == 1 and ups[0]["rating"] == "up"
        assert len(downs) == 1 and downs[0]["rating"] == "down"

    def test_count(self, fb: FeedbackManager):
        assert fb.count() == 0
        fb.add("m1", "alice", "up")
        assert fb.count() == 1


# ---------------- 反馈 API（接口层） ----------------

@pytest.fixture()
def client():
    return TestClient(app)


class TestFeedbackApi:
    def test_submit_feedback_requires_auth(self, client):
        resp = client.post("/api/chat/feedback", json={
            "message_id": "x", "rating": "up"})
        assert resp.status_code == 401

    def test_submit_unknown_message_404(self, client):
        # 免认证直连模式（内网部署），不存在消息应 404
        settings.security.auth_enabled = False
        try:
            resp = client.post("/api/chat/feedback", json={
                "message_id": "no_such_msg", "rating": "up"})
            assert resp.status_code == 404
        finally:
            settings.security.auth_enabled = True

    def test_submit_bad_rating_400(self, client):
        settings.security.auth_enabled = False
        try:
            resp = client.post("/api/chat/feedback", json={
                "message_id": "no_such_msg", "rating": "ok"})
            assert resp.status_code == 400
        finally:
            settings.security.auth_enabled = True

    def test_admin_feedback_list_requires_admin(self, client):
        settings.security.auth_enabled = False
        try:
            resp = client.get("/api/admin/feedback")
            # 免认证时为 system admin，应 200
            assert resp.status_code == 200
            assert "items" in resp.json()
        finally:
            settings.security.auth_enabled = True

    def test_admin_feedback_export_shape(self, client):
        settings.security.auth_enabled = False
        try:
            resp = client.get("/api/admin/feedback/export")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data and "name" in data
        finally:
            settings.security.auth_enabled = True


# ---------------- 回归脚本工具（评测集加载） ----------------

class TestRegressionUtils:
    def test_dataset_format(self):
        """黄金评测集格式: name/description/items[question/golden_answer/source_doc]"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from eval.run_regression import load_dataset
        items = load_dataset(with_feedback=False)
        assert len(items) > 0
        for it in items:
            assert it["question"] and it["golden_answer"]
            assert it["source_doc"]

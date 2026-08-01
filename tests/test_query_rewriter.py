"""Query 改写模块单元测试"""
import pytest

from app.core.query_rewriter import QueryRewriteService


@pytest.fixture
def rewriter():
    """测试实例（开启改写）"""
    return QueryRewriteService(enabled=True)


@pytest.fixture
def history():
    """典型多轮对话历史"""
    return [
        {"role": "user", "content": "什么是向量检索？"},
        {"role": "assistant", "content": "向量检索是通过语义相似度查找相关文档的技术。"},
        {"role": "user", "content": "它和关键词检索有什么区别？"},
        {"role": "assistant", "content": "向量检索基于语义，关键词检索基于字面匹配。"},
    ]


class TestNeedRewrite:
    """need_rewrite 判断逻辑"""

    def test_follow_up_pronoun(self, rewriter, history):
        """代词式追问需要改写"""
        assert rewriter.need_rewrite("那它的优势呢？", history) is True
        assert rewriter.need_rewrite("这个呢？", history) is True
        assert rewriter.need_rewrite("它有什么缺点？", history) is True

    def test_short_follow_up(self, rewriter, history):
        """短追问需要改写"""
        assert rewriter.need_rewrite("部署方式呢", history) is True
        assert rewriter.need_rewrite("还有呢", history) is True
        assert rewriter.need_rewrite("具体怎么做？", history) is True

    def test_self_contained_query(self, rewriter, history):
        """自包含完整问句不需要改写"""
        assert rewriter.need_rewrite("什么是BM25算法？", history) is False
        assert rewriter.need_rewrite("向量检索的优缺点是什么？", history) is False
        assert rewriter.need_rewrite("为什么混合检索效果更好？", history) is False

    def test_no_history(self, rewriter):
        """无历史时不需要改写"""
        assert rewriter.need_rewrite("那部署方式呢", []) is False

    def test_disabled(self, history):
        """关闭改写功能时不需要改写"""
        rw = QueryRewriteService(enabled=False)
        assert rw.need_rewrite("那部署方式呢", history) is False


class TestRewrite:
    """rewrite 调用 LLM 改写"""

    @pytest.mark.asyncio
    async def test_process_no_rewrite(self, rewriter):
        """自包含查询直接返回原样"""
        query, changed = await rewriter.process("什么是向量检索？", [])
        assert query == "什么是向量检索？"
        assert changed is False

    @pytest.mark.asyncio
    async def test_process_with_history_but_self_contained(self, rewriter, history):
        """有历史但查询自包含时不改写"""
        query, changed = await rewriter.process("什么是BM25算法？", history)
        assert query == "什么是BM25算法？"
        assert changed is False

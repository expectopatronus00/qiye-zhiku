"""评估模块单元测试 - Day 6 (RAGAS 本地裁判)"""
import pytest

from app.core.config import settings
from app.core.evaluator import (
    RAGASEvaluator,
    cosine_similarity,
    format_context,
    literal_check,
    normalize_numbers,
    parse_statements,
    parse_verdict,
)


# ---------------- 测试替身 ----------------

class FakeLLM:
    """按用户消息内容路由返回预设裁判输出"""

    def __init__(self, routes: dict[str, str] = None, default: str = "{}"):
        self.model = "fake-judge"
        self.routes = routes or {}
        self.default = default
        self.temperature_calls: list = []
        self.judge_model = "fake-judge"

    async def chat(self, messages, temperature=None):
        self.temperature_calls.append(temperature)
        user = messages[-1]["content"]
        for keyword, response in self.routes.items():
            if keyword in user:
                return response
        return self.default


class FakeEmbeddings:
    """关键词命中式假向量（确定性，便于断言相似度）"""

    def _vec(self, text: str) -> list[float]:
        if "温度" in text:
            return [1.0, 0.0, 0.0]
        if "告警" in text:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    async def embed_text(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


STATEMENTS_JSON = '{"statements": ["CPU温度正常区间是40-70℃", "告警阈值是大于85℃"]}'
VERDICT_YES = '{"verdict": "是"}'
VERDICT_NO = '{"verdict": "否"}'

FAKE_CONTEXTS = [
    {"content": "CPU温度正常区间是40-70℃。", "metadata": {"filename": "doc_a.pdf"}},
    {"content": "本方案对数据中心服务器进行7x24小时监控。", "metadata": {"filename": "doc_b.pdf"}},
]

# 不含任何数值片段，所有陈述判定都走 LLM 裁判路径
NO_NUM_CONTEXTS = [
    {"content": "当GPU利用率超过阈值时触发告警，并通过企业微信通知值班人员。",
     "metadata": {"filename": "doc_a.pdf"}},
]


def make_evaluator(llm_routes: dict[str, str] = None, llm_default: str = "{}"):
    """构造注入替身的评估器"""
    llm = FakeLLM(llm_routes, llm_default)
    evaluator = RAGASEvaluator(llm=llm, embeddings=FakeEmbeddings())
    return evaluator, llm


# ---------------- 解析工具 ----------------

class TestParsing:
    def test_parse_statements_json(self):
        assert parse_statements(STATEMENTS_JSON) == ["CPU温度正常区间是40-70℃", "告警阈值是大于85℃"]

    def test_parse_statements_codeblock(self):
        raw = '```json\n{"statements": ["A", "B"]}\n```'
        assert parse_statements(raw) == ["A", "B"]

    def test_parse_statements_line_fallback(self):
        raw = "1. CPU温度正常区间是40-70℃\n2. 告警阈值是大于85℃"
        assert parse_statements(raw) == ["CPU温度正常区间是40-70℃", "告警阈值是大于85℃"]

    def test_parse_statements_empty(self):
        assert parse_statements("") == []
        assert parse_statements("无法解析的内容") == []

    def test_parse_verdict_variants(self):
        assert parse_verdict(VERDICT_YES) is True
        assert parse_verdict(VERDICT_NO) is False
        assert parse_verdict('{"supported": true}') is True
        assert parse_verdict('{"supported": false}') is False
        assert parse_verdict("是") is True
        assert parse_verdict("否") is False
        assert parse_verdict("模型输出乱码123") is None
        assert parse_verdict("") is None


class TestVectorOps:
    def test_cosine_identical(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_cosine_orthogonal(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_cosine_empty(self):
        assert cosine_similarity([], []) == 0.0


class TestFormatContext:
    def test_join_with_source(self):
        text = format_context(FAKE_CONTEXTS, max_chars=100000)
        assert "[来源: doc_a.pdf]" in text
        assert "[来源: doc_b.pdf]" in text
        assert "---" in text

    def test_truncation(self):
        big = [{"content": "甲" * 5000, "metadata": {"filename": "big.pdf"}}]
        text = format_context(big, max_chars=1000)
        assert len(text) <= 1000
        assert "big.pdf" in text


class TestNormalizeNumbers:
    def test_comparison_words(self):
        assert normalize_numbers("告警阈值是大于85℃") == "告警阈值是>85C"
        assert normalize_numbers("正常区间低于60%") == "正常区间<60%"
        assert normalize_numbers("内存占用小于70%") == "内存占用<70%"
        assert normalize_numbers("超过90%触发告警") == ">90%触发告警"
        assert normalize_numbers("大于等于85%") == ">=85%"
        assert normalize_numbers("温度不超过40℃") == "温度<=40C"

    def test_no_change_when_plain(self):
        assert normalize_numbers("7x24小时监控") == "7x24小时监控"
        assert normalize_numbers("") == ""

    def test_degree_units(self):
        assert normalize_numbers("40-70°C 与 40-70℃") == "40-70C 与 40-70C"
        assert normalize_numbers("摄氏度") == "C"


class TestLiteralCheck:
    """数值事实字面预检"""

    def test_found_comparison(self):
        assert literal_check("磁盘IO告警阈值是>80%", "| 磁盘IO | <60% | >80% | 提示 |") is True

    def test_found_range(self):
        assert literal_check("CPU温度正常区间是40-70C", "| CPU温度 | 40-70C | >85C |") is True

    def test_found_unit_value(self):
        assert literal_check("功耗为220W", "GPU Utilization 85% 220W") is True

    def test_normalized_translation(self):
        # 中文比较词归一化后字面命中
        assert literal_check("告警阈值是大于85℃", "| CPU温度 | 40-70C | >85C |") is True
        assert literal_check("正常区间低于60%", "| 磁盘IO | <60% | >80% |") is True

    def test_missing_fragment_returns_none(self):
        assert literal_check("告警阈值是>85C", "| CPU温度 | 40-70C |") is None

    def test_no_numeric_returns_none(self):
        assert literal_check("通过企业微信通知值班人员", "企业微信通知") is None


# ---------------- 三大指标 ----------------

class TestFaithfulness:
    @pytest.mark.asyncio
    async def test_all_supported(self):
        evaluator, llm = make_evaluator(llm_routes={
            "抽取独立": STATEMENTS_JSON,
        }, llm_default=VERDICT_YES)
        result = await evaluator.faithfulness(
            "CPU温度正常区间是40-70℃，告警阈值是大于85℃。", FAKE_CONTEXTS)
        assert result["score"] == 1.0
        assert result["statements_total"] == 2
        assert result["statements_checked"] == 2
        assert result["statements_skipped"] == 0

    @pytest.mark.asyncio
    async def test_partial_support(self):
        evaluator, llm = make_evaluator(llm_routes={
            "抽取独立": STATEMENTS_JSON,
            "陈述：告警阈值是>85C": VERDICT_NO,  # 第二条不支持（陈述已被归一化）
        }, llm_default=VERDICT_YES)
        result = await evaluator.faithfulness("答案文本", FAKE_CONTEXTS)
        assert result["score"] == 0.5
        assert result["statements_supported"] == 1

    @pytest.mark.asyncio
    async def test_unparseable_verdict_skipped(self):
        evaluator, llm = make_evaluator(llm_routes={
            "抽取独立": STATEMENTS_JSON,
        }, llm_default="裁判输出异常，无法解析")
        # NO_NUM_CONTEXTS 无数值片段 → 两条陈述都走 LLM → 均无法解析 → 全部跳过
        result = await evaluator.faithfulness("答案文本", NO_NUM_CONTEXTS)
        assert result["statements_checked"] == 0
        assert result["statements_skipped"] == 2
        assert result["score"] == 0.0

    @pytest.mark.asyncio
    async def test_literal_check_bypasses_llm(self):
        evaluator, llm = make_evaluator(llm_routes={
            "抽取独立": STATEMENTS_JSON,
        }, llm_default=VERDICT_NO)  # LLM 一律判否，但数值命中应走字面预检
        result = await evaluator.faithfulness("答案文本", FAKE_CONTEXTS)
        # "40-70C" 字面命中 → True，不走 LLM；"告警阈值"陈述无数值命中走 LLM
        assert result["score"] == 0.5
        assert result["statements_supported"] == 1

    @pytest.mark.asyncio
    async def test_judge_uses_low_temperature(self):
        evaluator, llm = make_evaluator(llm_routes={"抽取独立": STATEMENTS_JSON},
                                        llm_default=VERDICT_YES)
        await evaluator.faithfulness("答案文本", FAKE_CONTEXTS)
        # 所有裁判调用都应使用配置的低温（默认 0.0，保证判定一致）
        assert all(t == settings.eval.judge_temperature for t in llm.temperature_calls)
        assert llm.temperature_calls


class TestAnswerRelevancy:
    @pytest.mark.asyncio
    async def test_mixed_similarity(self):
        # 问题含"温度"→vec[1,0,0]; 生成问题1含"温度"(sim 1.0), 问题2含"告警"(sim 0.0)
        evaluator, _ = make_evaluator(llm_routes={
            "反向生成": '{"questions": ["CPU温度的正常区间是多少？", "告警阈值是多少？"]}',
        })
        result = await evaluator.answer_relevancy(
            "CPU温度的正常区间是多少？", "CPU温度正常区间是40-70℃。")
        assert result["score"] == pytest.approx(0.5)
        assert len(result["questions"]) == 2
        assert result["similarities"] == [1.0, 0.0]

    @pytest.mark.asyncio
    async def test_no_questions_returns_zero(self):
        evaluator, _ = make_evaluator(llm_default="{}")
        result = await evaluator.answer_relevancy("问题", "答案")
        assert result["score"] == 0.0
        assert result["questions"] == []


class TestContextRecall:
    @pytest.mark.asyncio
    async def test_recall_score(self):
        evaluator, llm = make_evaluator(llm_routes={
            "抽取独立": STATEMENTS_JSON,
            "陈述：告警阈值是>85C": VERDICT_NO,  # 第二条未出现在上下文中（已归一化）
        }, llm_default=VERDICT_YES)
        result = await evaluator.context_recall(
            "CPU温度正常区间是40-70℃，告警阈值是大于85℃。", FAKE_CONTEXTS)
        assert result["score"] == 0.5
        assert result["statements_present"] == 1


class TestEvaluateItem:
    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        evaluator, _ = make_evaluator(llm_routes={
            "抽取独立": STATEMENTS_JSON,
            "反向生成": '{"questions": ["CPU温度的正常区间是多少？"]}',
        }, llm_default=VERDICT_YES)
        result = await evaluator.evaluate_item(
            question="CPU温度正常区间是多少？",
            golden_answer="CPU温度正常区间是40-70℃。",
            contexts=FAKE_CONTEXTS,
            answer="CPU温度正常区间是40-70℃。",
        )
        assert set(result.keys()) == {
            "question", "answer", "faithfulness", "answer_relevancy", "context_recall",
        }
        assert result["faithfulness"]["score"] == 1.0
        assert result["answer_relevancy"]["score"] == 1.0  # 问题与生成问题都含"温度"
        assert result["context_recall"]["score"] == 1.0

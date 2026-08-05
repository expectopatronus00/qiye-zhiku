"""RAGAS 评估模块 - 使用本地 LLM 作为裁判

Day 6 (v0.6) 评估体系，遵循 RAGAS 方法论，但裁判全程使用本地模型，
保持"数据不出域"的私有化定位，无需调用任何外部评估 API：

- faithfulness (忠实度): 答案中的陈述能否在检索上下文中找到依据
  (答案拆陈述 → 逐条判定是否被上下文支持 → 支持数/总数)
- answer_relevancy (答案相关性): 答案是否切题
  (由答案反向生成问题 → 与原问题做向量余弦相似度 → 取均值)
- context_recall (上下文召回率): 检索到的上下文是否覆盖黄金答案
  (黄金答案拆陈述 → 逐条判定是否出现在上下文中 → 出现数/总数，
  需要标注了黄金答案的评测集)

裁判输出统一要求 JSON，解析失败时自动降级为文本规则解析，
仍无法判定时跳过该条（不计入分母），保证评估流程不中断。
"""
import json
import re
from typing import Optional

import numpy as np

from app.core.config import settings
from app.core.embeddings import EmbeddingService
from app.core.llm import LLMService


# ---------------- 裁判提示词 ----------------

JUDGE_SYSTEM_PROMPT = (
    "你是一名严谨的 RAG 系统质量评估裁判。你的所有回答必须只输出一个 JSON 对象，"
    "不要输出任何其他文字、解释或代码块标记。"
)

STATEMENTS_PROMPT = """请从下面这段文本中抽取独立的、可验证的陈述句（事实断言）。
要求：
1. 每个陈述必须是单句事实断言，不含推测、建议或祈使句
2. 保留具体数字、单位、名称等关键信息
3. 合并同义重复内容，不要拆分纯并列短语
4. 输出格式：{{"statements": ["陈述1", "陈述2", ...]}}

文本：
{text}"""

SUPPORT_PROMPT = (
    '请判断下面的"陈述"是否被"参考上下文"明确支持（可以直接推断得出）。\n'
    "参考上下文中的内容视为事实，不依赖常识与外部知识。\n"
    "判定时忽略表述差异：数字、单位、符号含义一致即视为相同信息\n"
    "（例如 \">85C\" 与 \"大于85℃\"、\"<60%\" 与 \"低于60%\" 含义相同）。\n"
    '只输出 JSON：{{"verdict": "是"}} 或 {{"verdict": "否"}}\n'
    "\n参考上下文：\n{context}\n\n陈述：{statement}\n\n"
    '{{"verdict": '
)

PRESENCE_PROMPT = (
    '请判断下面的"陈述"所描述的内容是否在"参考上下文"中出现或可被直接推断。\n'
    "只要上下文包含该陈述的全部关键信息即为出现，不要求措辞一致。\n"
    "判定时忽略表述差异：数字、单位、符号含义一致即视为相同信息\n"
    "（例如 \">85C\" 与 \"大于85℃\"、\"<60%\" 与 \"低于60%\" 含义相同）。\n"
    '只输出 JSON：{{"verdict": "是"}} 或 {{"verdict": "否"}}\n'
    "\n参考上下文：\n{context}\n\n陈述：{statement}\n\n"
    '{{"verdict": '
)

QUESTIONS_PROMPT = """请根据下面这段"答案"文本，反向生成 {num} 个不同角度的、用户可能会问的问题。
要求：
1. 问题必须仅凭该答案就能回答（不引入外部知识）
2. 覆盖答案中的不同信息点（数字、名称、流程等）
3. 问题用中文表述，简洁自然
输出格式：{{"questions": ["问题1", "问题2", ...]}}

答案：
{answer}"""


# ---------------- 解析工具 ----------------

def _extract_json_object(raw: str) -> Optional[dict]:
    """从模型输出中提取第一个 JSON 对象"""
    if not raw:
        return None
    raw = raw.strip()
    # 去掉可能的 ```json ``` 代码块
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    # 直接解析
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # 用大括号定位第一个完整对象
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def parse_statements(raw: str) -> list[str]:
    """解析陈述/问题列表，JSON 优先，降级为行拆分"""
    obj = _extract_json_object(raw)
    if obj:
        for key in ("statements", "questions", "items", "list"):
            value = obj.get(key)
            if isinstance(value, list):
                cleaned = [str(v).strip().strip("\"'。") for v in value if str(v).strip()]
                return cleaned
    # 降级：按行拆分，去掉序号/列表符/引号
    # 仅当输出呈现"列表形态"(多行或带列表符)时才启用，避免把乱码当陈述
    has_list_marker = bool(re.search(r"(^|\n)\s*(?:[-*•·]|\d+[.、)])", raw))
    if "\n" not in raw and not has_list_marker:
        return []
    lines = []
    for line in raw.splitlines():
        line = line.strip().strip("*•-·").strip()
        line = re.sub(r"^\d+[.、)]\s*", "", line).strip()
        line = line.strip("\"'“”")
        if len(line) >= 4 and line not in lines:
            lines.append(line)
    return lines


def parse_verdict(raw: str) -> Optional[bool]:
    """解析是/否判定，无法判定返回 None（跳过该条）"""
    obj = _extract_json_object(raw)
    if obj:
        verdict = obj.get("verdict")
        if isinstance(verdict, bool):
            return verdict
        if isinstance(verdict, str):
            if "否" in verdict or "不" in verdict:
                return False
            if "是" in verdict or "支持" in verdict:
                return True
        # 兼容 {"supported": true/false}
        for key in ("supported", "yes", "present", "found"):
            if key in obj:
                v = obj[key]
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return "否" not in v and "不" not in v and ("是" in v or "支" in v or "出" in v or "找" in v)
    # 降级：文本包含判定词
    if "否" in raw or "不支持" in raw or "未找到" in raw or "未出现" in raw:
        return False
    if "是" in raw or "支持" in raw or "出现" in raw or "找到" in raw:
        return True
    return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """向量余弦相似度"""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.size == 0 or b.size == 0:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


# 数值表述归一化（裁判判定前统一符号/单位表述，避免 7B 模型对
# "大于85℃" 与 ">85C" 这类同义表述做翻译推理导致误判）
# 注意：否定形式（不超过/不低于等）必须先于肯定形式替换
_NUM_REPLACEMENTS = [
    ("大于等于", ">="), ("不小于", ">="), ("不低于", ">="), ("不少于", ">="),
    ("至少", ">="),
    ("小于等于", "<="), ("不大于", "<="), ("不高于", "<="), ("不超过", "<="),
    ("不多于", "<="), ("至多", "<="),
    ("大于", ">"), ("超过", ">"), ("高于", ">"), ("以上", ">"),
    ("小于", "<"), ("低于", "<"), ("不足", "<"), ("以下", "<"),
    ("摄氏度", "C"), ("°C", "C"), ("℃", "C"),
]


def normalize_numbers(text: str) -> str:
    """统一数值比较词与温度单位，便于裁判做字面判定

    "告警阈值是大于85℃" -> "告警阈值是>85C"；"低于60%" -> "<60%"
    """
    if not text:
        return text
    for src, dst in _NUM_REPLACEMENTS:
        text = text.replace(src, dst)
    return text


# 数值事实片段（比较式 / 区间式 / 带单位数值），用于确定性字面预检
_NUM_FRAGMENT_RE = re.compile(
    r"[<>]=?\s*[-+]?\d+(?:\.\d+)?(?:%|℃|°C|C|ms|rpm|GB|MB|TB|GHz|MHz|W|V)?"
    r"|[-+]?\d+(?:\.\d+)?\s*[-~～]\s*[-+]?\d+(?:\.\d+)?(?:%|℃|°C|C|ms|rpm|GB|MB|TB|GHz|MHz|W|V)?"
    r"|[-+]?\d+(?:\.\d+)?(?:%|℃|°C|C|ms|rpm|GB|MB|TB|GHz|MHz|W|V)(?![\w.])"
)


def literal_check(statement: str, context: str) -> Optional[bool]:
    """数值事实的确定性字面预检（LLM 裁判前的快速通道）

    陈述中的数值片段（如 ">85C"、"40-70C"、"220W"）全部在上下文中
    出现时直接判定为"是"——数值事实用字面校验比 7B 裁判更稳定。
    内部自动做数值表述归一化（"大于85℃" -> ">85C"）。
    陈述不含数值片段或片段未完全命中时返回 None，交由 LLM 判定。
    """
    statement = normalize_numbers(statement)
    context = normalize_numbers(context)
    fragments = [re.sub(r"\s+", "", f) for f in _NUM_FRAGMENT_RE.findall(statement)]
    if not fragments:
        return None
    context_compact = re.sub(r"\s+", "", context)
    if all(f in context_compact for f in fragments):
        return True
    return None


def format_context(contexts: list[dict], max_chars: int = 4000) -> str:
    """格式化检索上下文供裁判判定（与 chat 路由一致的来源标注格式）"""
    parts = []
    total = 0
    for doc in contexts:
        filename = doc.get("metadata", {}).get("filename", "未知")
        content = doc.get("content", "")
        block = f"[来源: {filename}]\n{content}"
        if total + len(block) > max_chars:
            remain = max_chars - total
            if remain > 100:
                parts.append(block[:remain])
            break
        parts.append(block)
        total += len(block)
    return "\n\n---\n\n".join(parts)


# ---------------- 评估器 ----------------

class RAGASEvaluator:
    """RAGAS 兼容评估器（本地裁判）

    三个指标均返回 dict，包含 score 与明细，便于报告与排查：
    {"score": float, ...明细字段}
    """

    def __init__(
        self,
        llm: Optional[LLMService] = None,
        embeddings: Optional[EmbeddingService] = None,
    ):
        self.llm = llm or LLMService()
        self.embeddings = embeddings or EmbeddingService()
        self.judge_model = settings.eval.judge_model or self.llm.model
        self.judge_temperature = settings.eval.judge_temperature
        self.num_questions = settings.eval.num_generated_questions
        self.max_context_chars = settings.eval.max_context_chars
        # 裁判用独立 LLMService（配置了 judge_model），避免篡改共享实例
        self._judge_llm = self.llm
        if self.judge_model != self.llm.model:
            judge_llm = LLMService()
            judge_llm.model = self.judge_model
            self._judge_llm = judge_llm

    # ---------- 基础裁判调用 ----------

    async def _judge(self, user_prompt: str) -> str:
        """调用裁判模型（固定低温，保证判定一致性）"""
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        return await self._judge_llm.chat(messages, temperature=self.judge_temperature)

    async def generate_statements(self, text: str) -> list[str]:
        """从文本中抽取独立陈述句"""
        raw = await self._judge(STATEMENTS_PROMPT.format(text=text))
        return parse_statements(raw)

    async def generate_questions(self, answer: str, num: Optional[int] = None) -> list[str]:
        """由答案反向生成可能的问题（相关性评估用）"""
        raw = await self._judge(QUESTIONS_PROMPT.format(
            answer=answer, num=num or self.num_questions,
        ))
        return parse_statements(raw)

    async def _check_support(self, statement: str, context: str) -> Optional[bool]:
        """判定陈述是否被上下文支持（忠实度）

        先做数值字面预检（确定性），未决的交给 LLM 裁判
        """
        context = context[:self.max_context_chars]
        literal = literal_check(normalize_numbers(statement), normalize_numbers(context))
        if literal is not None:
            return literal
        raw = await self._judge(SUPPORT_PROMPT.format(
            context=normalize_numbers(context), statement=normalize_numbers(statement),
        ))
        return parse_verdict(raw)

    async def _check_presence(self, statement: str, context: str) -> Optional[bool]:
        """判定陈述内容是否出现在上下文中（召回率）

        先做数值字面预检（确定性），未决的交给 LLM 裁判
        """
        context = context[:self.max_context_chars]
        literal = literal_check(normalize_numbers(statement), normalize_numbers(context))
        if literal is not None:
            return literal
        raw = await self._judge(PRESENCE_PROMPT.format(
            context=normalize_numbers(context), statement=normalize_numbers(statement),
        ))
        return parse_verdict(raw)

    # ---------- 三大指标 ----------

    async def faithfulness(self, answer: str, contexts: list[dict]) -> dict:
        """忠实度：答案陈述被上下文支持的比例"""
        context = format_context(contexts, self.max_context_chars)
        statements = await self.generate_statements(answer)
        supported, total, skipped = 0, 0, 0
        verdicts = []
        for stmt in statements:
            verdict = await self._check_support(stmt, context)
            verdicts.append({"statement": stmt, "supported": verdict})
            if verdict is None:
                skipped += 1
                continue
            total += 1
            supported += int(verdict)
        score = round(supported / total, 4) if total > 0 else 0.0
        return {
            "score": score,
            "statements_total": len(statements),
            "statements_supported": supported,
            "statements_checked": total,
            "statements_skipped": skipped,
            "verdicts": verdicts,
        }

    async def answer_relevancy(self, question: str, answer: str) -> dict:
        """答案相关性：由答案生成的问题与原问题的向量相似度均值"""
        questions = await self.generate_questions(answer)
        if not questions:
            return {"score": 0.0, "questions": [], "similarities": []}
        q_vec = await self.embeddings.embed_query(question)
        similarities = []
        for q in questions:
            g_vec = (await self.embeddings.embed_text([q]))[0]
            similarities.append(round(cosine_similarity(q_vec, g_vec), 4))
        score = round(float(np.mean(similarities)), 4) if similarities else 0.0
        return {
            "score": score,
            "questions": questions,
            "similarities": similarities,
        }

    async def context_recall(self, golden_answer: str, contexts: list[dict]) -> dict:
        """上下文召回率：黄金答案陈述被检索上下文覆盖的比例"""
        context = format_context(contexts, self.max_context_chars)
        statements = await self.generate_statements(golden_answer)
        present, total, skipped = 0, 0, 0
        verdicts = []
        for stmt in statements:
            verdict = await self._check_presence(stmt, context)
            verdicts.append({"statement": stmt, "present": verdict})
            if verdict is None:
                skipped += 1
                continue
            total += 1
            present += int(verdict)
        score = round(present / total, 4) if total > 0 else 0.0
        return {
            "score": score,
            "statements_total": len(statements),
            "statements_present": present,
            "statements_checked": total,
            "statements_skipped": skipped,
            "verdicts": verdicts,
        }

    async def evaluate_item(
        self,
        question: str,
        golden_answer: str,
        contexts: list[dict],
        answer: str,
    ) -> dict:
        """对单个样本运行三项评估

        contexts: 检索到的上下文列表（含 content/metadata）
        answer:   RAG 系统对 question 的实际回答
        """
        faithful = await self.faithfulness(answer, contexts)
        relevant = await self.answer_relevancy(question, answer)
        recall = await self.context_recall(golden_answer, contexts)
        return {
            "question": question,
            "answer": answer,
            "faithfulness": faithful,
            "answer_relevancy": relevant,
            "context_recall": recall,
        }

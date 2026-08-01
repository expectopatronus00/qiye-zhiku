"""Query 改写模块 - 多轮对话上下文补全

解决多轮对话中"追问"检索失效的问题：
用户连续提问时，后续问题常是省略式（如"那部署方式呢"、"第二个方案呢"），
直接拿去检索会得到垃圾结果。本模块先判断是否需要改写，
需要时让 LLM 基于对话历史补全为独立、完整的检索 Query。
"""
import re
from app.core.llm import LLMService
from app.core.config import settings


# 判断是否"依赖上下文"的启发式规则
_CONTEXT_DEPENDENT_PATTERNS = [
    r"^(那|那么|这个|那个|这|它|他|她|其|该|上面|刚才|前面|上一条).{0,15}[呢吗？?]?$",
    r"^.{0,6}(呢|么|怎么样|如何|咋|呢？)$",          # 短追问："部署呢？""价格呢？"
    r"^(还有|另外|其他|别的).{0,10}$",
    r"^(为什么|为啥|凭什么).{0,8}$",                  # 承接上文的"为什么"
    r"^(具体|详细|展开|接着说|继续|往下).{0,10}$",
]

_SHORT_QUERY_LEN = 12  # 过短的独立问题大概率是追问


class QueryRewriteService:
    """查询改写服务"""

    def __init__(self, enabled: bool = True, model: str = ""):
        self.enabled = enabled
        self.llm = LLMService()
        if model:
            self.llm.model = model

    def need_rewrite(self, query: str, history: list[dict]) -> bool:
        """判断当前查询是否需要改写（纯规则判断，不消耗 LLM）"""
        if not self.enabled:
            return False
        if not history:  # 没有历史就不需要改写
            return False

        query = query.strip()
        if not query:
            return False

        # 规则 1: 命中明显依赖上下文的句式
        for pattern in _CONTEXT_DEPENDENT_PATTERNS:
            if re.match(pattern, query):
                return True

        # 规则 2: 过短且不是完整问句（无明确主语/对象）
        if len(query) <= _SHORT_QUERY_LEN and not self._is_self_contained(query):
            return True

        return False

    @staticmethod
    def _is_self_contained(query: str) -> bool:
        """粗略判断是否为自包含完整问句（含明确技术词或完整主谓宾）"""
        # 包含具体疑问词且句长足够 → 通常自包含
        if re.search(r"(什么是|是什么|怎么|如何|多少|哪个|哪些|区别|介绍|说明|为什么|有几|有哪些)", query):
            # 但有具体对象才自包含，如"什么是向量检索" 自包含，"这个呢" 不
            return len(query) > 8
        return False

    async def rewrite(self, query: str, history: list[dict]) -> str:
        """基于对话历史改写查询为独立完整 Query"""
        # 构造精简历史（最后 4 轮）
        recent = history[-8:]
        history_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:120]}"
            for m in recent
        )

        prompt = f"""你是信息检索查询改写专家。用户在多轮对话中提出了一个新问题，请把它改写为一条"独立、完整、可单独检索"的检索查询。

要求：
1. 结合对话历史理解新问题指代的对象（"它/那个/这种方式"等指代要还原）
2. 补充被省略的技术名词、主语、限定条件
3. 保持原问题意图不变，不要添加历史之外的信息
4. 输出仅一条改写后的查询，不要任何解释、引号或前缀

对话历史：
{history_text}

当前问题：{query}

改写后的独立查询："""

        messages = [
            {"role": "system", "content": "你是信息检索查询改写专家，只输出改写结果本身。"},
            {"role": "user", "content": prompt},
        ]

        try:
            result = await self.llm.chat(messages)
            result = result.strip().strip('"').strip("'").strip()
            # 防御：改写结果异常时回退原查询
            if not result or len(result) > 200 or "\n" in result:
                return query
            return result
        except Exception:
            # LLM 不可用时回退原查询
            return query

    async def process(self, query: str, history: list[dict]) -> tuple[str, bool]:
        """统一入口：返回 (最终查询, 是否发生了改写)"""
        if self.need_rewrite(query, history):
            rewritten = await self.rewrite(query, history)
            return rewritten, rewritten != query
        return query, False


# 全局实例（启用状态从配置读取）
query_rewriter = QueryRewriteService(enabled=settings.query_rewrite.enabled)

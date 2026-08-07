"""Agent 执行引擎 (v0.9) - 工具调用循环 + 多步推理

流程：LLM 决策 → 执行工具 → 结果回填 → 再次决策，直到给出最终答案或达到迭代上限。
工具执行失败不会中断循环，而是作为错误结果回填给模型（模型可换工具/换参数重试）。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from app.core.config import settings
from app.core.llm import LLMResponse, LLMService
from app.core.tools import tools as tool_registry

# Agent 系统提示词（央企知识库场景）
AGENT_SYSTEM_PROMPT = """你是一个专业的企业知识库智能助手（Agent 模式）。
你可以调用工具来获取信息，自主决定需要哪些信息、按什么顺序获取，然后综合推理回答用户问题。

规则：
1. 优先使用 search_knowledge_base 检索相关知识，再回答；信息不足时可调用 preview_document 精读全文
2. 回答必须基于工具返回的真实资料，不得编造；引用来源时标注文件名
3. 多步任务可以连续调用多个工具（如先 list_knowledge_bases / knowledge_base_stats 摸清范围，再检索或精读）
4. 工具调用出错时，根据错误信息调整参数重试，或改用其他工具
5. 工具已返回数据时（如知识库列表、统计数字、文档清单），必须直接基于这些真实数据回答，例如明确列出知识库名称、文档数量、文件名；禁止无视已有结果回答"未找到相关信息"
6. 最终回答要求专业、准确、简洁，并列出依据的文件名
7. 若所有工具都确实无法获取相关信息，明确告知"根据现有知识库资料，未找到相关答案"
"""


class AgentStep:
    """单次工具调用记录（用于前端展示思维链）"""

    def __init__(self, tool_name: str, arguments: dict, result: Any, duration: float):
        self.tool_name = tool_name
        self.arguments = arguments
        self.result = result
        self.duration = duration

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "result": self.result,
            "duration_ms": round(self.duration * 1000),
        }


class AgentRun:
    """一次 Agent 任务执行"""

    def __init__(self, user=None, collection: str = "default",
                 max_iterations: Optional[int] = None):
        self.user = user
        self.collection = collection
        self.max_iterations = max_iterations or settings.agent.max_iterations
        self.steps: list[AgentStep] = []
        self.llm = LLMService()

    def _tool_message(self, content: str) -> dict:
        return {"role": "tool", "content": content}

    def _assistant_tool_message(self, response: LLMResponse) -> dict:
        """构造带 tool_calls 的 assistant 消息

        参数格式差异：Ollama 要求 arguments 为对象，OpenAI 要求 JSON 字符串。
        """
        import json
        msg: dict = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            calls = []
            for c in response.tool_calls:
                fn: dict = {"name": c.name, "arguments": c.arguments}
                if self.llm.provider == "openai":
                    fn["arguments"] = json.dumps(c.arguments, ensure_ascii=False)
                calls.append({"function": fn})
            msg["tool_calls"] = calls
        return msg

    def _summarize_result(self, result: Any, limit: int = 1500) -> str:
        """工具结果序列化（截断，避免上下文爆炸）"""
        try:
            import json
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)
        except Exception:
            result = str(result)
        return result[:limit]

    async def run(self, question: str, history: Optional[list[dict]] = None) -> dict:
        """执行 Agent 任务，返回 {answer, steps, source_files}"""
        messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": question})

        schemas = tool_registry.schemas()
        source_files: set[str] = set()
        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1
            response = await self.llm.chat_with_tools(messages, schemas)

            if not response.has_tool_calls:
                return {"answer": response.content, "steps": [s.to_dict() for s in self.steps],
                        "source_files": sorted(source_files), "iterations": iteration}

            # 回填 assistant 工具调用消息
            messages.append(self._assistant_tool_message(response))

            for call in response.tool_calls:
                t0 = time.time()
                error = None
                try:
                    result = await tool_registry.execute(
                        call.name, call.arguments,
                        deps={"user": self.user, "collection": self.collection},
                    )
                except Exception as exc:  # 权限/工具不存在等
                    error = f"工具调用失败: {exc}"
                    result = {"error": error}

                # 收集来源文件（用于最终标注）
                if isinstance(result, dict):
                    fn = result.get("filename")
                    if fn:
                        source_files.add(fn)
                    for hit in result.get("hits", []) or []:
                        if hit.get("filename"):
                            source_files.add(hit["filename"])
                    for doc in result.get("documents", []) or []:
                        if doc.get("filename"):
                            source_files.add(doc["filename"])

                self.steps.append(AgentStep(call.name, call.arguments, result, time.time() - t0))
                messages.append(self._tool_message(self._summarize_result(result)))

        # 达到迭代上限：让模型基于已有工具结果做最终总结
        messages.append({
            "role": "user",
            "content": "你已达到工具调用次数上限。请基于目前已获取的资料，直接给出最终回答；"
                       "资料不足时如实说明。",
        })
        final = await self.llm.chat(messages)
        return {"answer": final, "steps": [s.to_dict() for s in self.steps],
                "source_files": sorted(source_files), "iterations": iteration}


# ---------------- 便捷入口 ----------------

async def run_agent(question: str, user, collection: str = "default",
                    history: Optional[list[dict]] = None) -> dict:
    """Agent 模式统一入口（chat 路由调用）"""
    run = AgentRun(user=user, collection=collection)
    return await run.run(question, history)

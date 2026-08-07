"""LLM 调用模块 - 支持 Ollama / OpenAI（含 Function Calling / Tools API）"""
from typing import AsyncGenerator, Optional
import httpx
from app.core.config import settings


class LLMToolCall:
    """模型请求的工具调用"""
    def __init__(self, name: str, arguments: dict):
        self.name = name
        self.arguments = arguments


class LLMResponse:
    """带工具调用的模型响应"""
    def __init__(self, content: str, tool_calls: list[LLMToolCall] | None = None):
        self.content = content or ""
        self.tool_calls = tool_calls or []

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMService:
    """大语言模型服务"""

    def __init__(self):
        self.provider = settings.llm.provider
        self.model = settings.llm.model

    async def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        """发送对话请求，返回完整响应

        temperature 覆盖配置值（如评估裁判固定使用低温），None 则用配置
        """
        if self.provider == "ollama":
            return await self._chat_ollama(messages, temperature)
        elif self.provider == "openai":
            return await self._chat_openai(messages, temperature)
        else:
            raise ValueError(f"不支持的 LLM 提供者: {self.provider}")

    async def chat_stream(self, messages: list[dict], temperature: Optional[float] = None) -> AsyncGenerator[str, None]:
        """流式对话请求"""
        if self.provider == "ollama":
            async for chunk in self._chat_ollama_stream(messages, temperature):
                yield chunk
        elif self.provider == "openai":
            async for chunk in self._chat_openai_stream(messages, temperature):
                yield chunk

    async def chat_with_tools(self, messages: list[dict], tools: list[dict],
                              temperature: Optional[float] = None) -> LLMResponse:
        """带工具（Function Calling）的对话请求（非流式）

        tools: OpenAI 兼容的 function schema 列表。
        返回 LLMResponse，其中 tool_calls 已解析为 LLMToolCall。
        """
        if self.provider == "ollama":
            return await self._chat_ollama_tools(messages, tools, temperature)
        elif self.provider == "openai":
            return await self._chat_openai_tools(messages, tools, temperature)
        else:
            raise ValueError(f"不支持的 LLM 提供者: {self.provider}")

    def _resolve_temperature(self, temperature: Optional[float]) -> float:
        """解析温度：显式传入优先，否则用配置值"""
        return settings.llm.temperature if temperature is None else temperature

    async def _chat_ollama(self, messages: list[dict], temperature: Optional[float]) -> str:
        """Ollama 同步对话"""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "http://localhost:11434/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": self._resolve_temperature(temperature),
                        "num_predict": settings.llm.max_tokens,
                    },
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["message"]["content"]

    async def _chat_ollama_stream(self, messages: list[dict], temperature: Optional[float]) -> AsyncGenerator[str, None]:
        """Ollama 流式对话"""
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                "http://localhost:11434/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "options": {
                        "temperature": self._resolve_temperature(temperature),
                        "num_predict": settings.llm.max_tokens,
                    },
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        import json
                        data = json.loads(line)
                        if "message" in data and "content" in data["message"]:
                            yield data["message"]["content"]

    @staticmethod
    def _parse_tool_arguments(raw) -> dict:
        """解析工具参数：Ollama 返回 JSON 字符串，OpenAI 返回 dict"""
        import json
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    async def _chat_ollama_tools(self, messages: list[dict], tools: list[dict],
                                 temperature: Optional[float]) -> LLMResponse:
        """Ollama 工具调用（/api/chat 的 tools 参数）"""
        import json
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "http://localhost:11434/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "stream": False,
                    "options": {
                        "temperature": self._resolve_temperature(temperature),
                        "num_predict": settings.llm.max_tokens,
                    },
                },
            )
            response.raise_for_status()
            data = response.json()
            message = data.get("message", {})
            tool_calls = []
            for call in message.get("tool_calls", []) or []:
                fn = call.get("function", {})
                tool_calls.append(LLMToolCall(
                    name=fn.get("name", ""),
                    arguments=self._parse_tool_arguments(fn.get("arguments", {})),
                ))
            return LLMResponse(content=message.get("content", ""), tool_calls=tool_calls)

    async def _chat_openai_tools(self, messages: list[dict], tools: list[dict],
                                 temperature: Optional[float]) -> LLMResponse:
        """OpenAI 工具调用（chat/completions 的 tools 参数）"""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{settings.llm.openai_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.llm.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "temperature": self._resolve_temperature(temperature),
                    "max_tokens": settings.llm.max_tokens,
                },
            )
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]
            tool_calls = []
            for call in message.get("tool_calls", []) or []:
                fn = call.get("function", {})
                tool_calls.append(LLMToolCall(
                    name=fn.get("name", ""),
                    arguments=self._parse_tool_arguments(fn.get("arguments", {})),
                ))
            return LLMResponse(content=message.get("content", ""), tool_calls=tool_calls)

    async def _chat_openai(self, messages: list[dict], temperature: Optional[float]) -> str:
        """OpenAI 同步对话"""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{settings.llm.openai_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.llm.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self._resolve_temperature(temperature),
                    "max_tokens": settings.llm.max_tokens,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]

    async def _chat_openai_stream(self, messages: list[dict], temperature: Optional[float]) -> AsyncGenerator[str, None]:
        """OpenAI 流式对话"""
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{settings.llm.openai_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.llm.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self._resolve_temperature(temperature),
                    "max_tokens": settings.llm.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        line = line[6:]
                        if line == "[DONE]":
                            break
                        import json
                        data = json.loads(line)
                        delta = data["choices"][0].get("delta", {})
                        if "content" in delta:
                            yield delta["content"]

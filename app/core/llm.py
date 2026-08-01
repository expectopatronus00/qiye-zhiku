"""LLM 调用模块 - 支持 Ollama / OpenAI"""
from typing import AsyncGenerator
import httpx
from app.core.config import settings


class LLMService:
    """大语言模型服务"""

    def __init__(self):
        self.provider = settings.llm.provider
        self.model = settings.llm.model

    async def chat(self, messages: list[dict]) -> str:
        """发送对话请求，返回完整响应"""
        if self.provider == "ollama":
            return await self._chat_ollama(messages)
        elif self.provider == "openai":
            return await self._chat_openai(messages)
        else:
            raise ValueError(f"不支持的 LLM 提供者: {self.provider}")

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """流式对话请求"""
        if self.provider == "ollama":
            async for chunk in self._chat_ollama_stream(messages):
                yield chunk
        elif self.provider == "openai":
            async for chunk in self._chat_openai_stream(messages):
                yield chunk

    async def _chat_ollama(self, messages: list[dict]) -> str:
        """Ollama 同步对话"""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "http://localhost:11434/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": settings.llm.temperature,
                        "num_predict": settings.llm.max_tokens,
                    },
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["message"]["content"]

    async def _chat_ollama_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
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
                        "temperature": settings.llm.temperature,
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

    async def _chat_openai(self, messages: list[dict]) -> str:
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
                    "temperature": settings.llm.temperature,
                    "max_tokens": settings.llm.max_tokens,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]

    async def _chat_openai_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
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
                    "temperature": settings.llm.temperature,
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

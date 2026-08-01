"""向量嵌入模块 - 支持 Ollama / OpenAI / 本地模型"""
from typing import Optional
import httpx
from app.core.config import settings


class EmbeddingService:
    """向量嵌入服务"""

    def __init__(self):
        self.provider = settings.embedding.provider
        self.model = settings.embedding.model

    async def embed_text(self, texts: list[str]) -> list[list[float]]:
        """将文本列表转换为向量列表"""
        if self.provider == "ollama":
            return await self._embed_ollama(texts)
        elif self.provider == "openai":
            return await self._embed_openai(texts)
        else:
            raise ValueError(f"不支持的嵌入提供者: {self.provider}")

    async def embed_query(self, query: str) -> list[float]:
        """将单个查询文本转换为向量"""
        vectors = await self.embed_text([query])
        return vectors[0]

    async def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        """通过 Ollama API 生成嵌入"""
        vectors = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            for text in texts:
                response = await client.post(
                    "http://localhost:11434/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                response.raise_for_status()
                data = response.json()
                vectors.append(data["embedding"])
        return vectors

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        """通过 OpenAI API 生成嵌入"""
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.embedding.openai_base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {settings.embedding.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]

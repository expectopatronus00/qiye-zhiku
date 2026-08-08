"""向量嵌入模块 - 支持 Ollama / OpenAI / 国产化 provider（昇腾 CANN / 寒武纪 MLU / 摩尔线程）

v1.3 信创适配：ascend/cambricon/mthreads 走 OpenAI 兼容 /embeddings 端点
（国产推理栈上运行 bge-m3 等开源嵌入模型），base_url 按各自字段路由。
"""
from typing import Optional
import httpx
from app.core.config import settings, validate_provider, VALID_EMBEDDING_PROVIDERS

# 走 OpenAI 兼容 /embeddings 端点的 provider 集合
OPENAI_COMPAT_PROVIDERS = {"openai", "ascend", "cambricon", "mthreads"}


def resolve_embedding_base_url() -> str:
    """解析 OpenAI 兼容嵌入端点 base_url：按国产 provider 各自字段路由，未配置则回退 openai_base_url"""
    provider = settings.embedding.provider
    if provider == "ascend":
        return settings.embedding.ascend_base_url or settings.embedding.openai_base_url
    if provider == "cambricon":
        return settings.embedding.cambricon_base_url or settings.embedding.openai_base_url
    if provider == "mthreads":
        return settings.embedding.mthreads_base_url or settings.embedding.openai_base_url
    return settings.embedding.openai_base_url


class EmbeddingService:
    """向量嵌入服务"""

    def __init__(self):
        self.provider = settings.embedding.provider
        self.model = settings.embedding.model
        self.openai_api_key = settings.embedding.openai_api_key
        self.ascend_base_url = settings.embedding.ascend_base_url
        self.cambricon_base_url = settings.embedding.cambricon_base_url
        self.mthreads_base_url = settings.embedding.mthreads_base_url
        validate_provider(self.provider, VALID_EMBEDDING_PROVIDERS, "嵌入")

    def _is_openai_compat(self) -> bool:
        return self.provider in OPENAI_COMPAT_PROVIDERS

    async def embed_text(self, texts: list[str]) -> list[list[float]]:
        """将文本列表转换为向量列表"""
        if self.provider == "ollama":
            return await self._embed_ollama(texts)
        elif self._is_openai_compat():
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
                    f"{settings.llm.ollama_base_url}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                response.raise_for_status()
                data = response.json()
                vectors.append(data["embedding"])
        return vectors

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        """通过 OpenAI 兼容端点生成嵌入（国产 provider 复用此通道）"""
        headers = {"Content-Type": "application/json"}
        if self.openai_api_key:
            headers["Authorization"] = f"Bearer {self.openai_api_key}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{resolve_embedding_base_url()}/embeddings",
                headers=headers,
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]

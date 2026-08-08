"""v1.3 信创适配测试：国产 provider 路由/校验 + Milvus 后端 + 向量库工厂"""
import json

import pytest

from app.core.config import (
    settings,
    validate_provider,
    VALID_LLM_PROVIDERS,
    VALID_EMBEDDING_PROVIDERS,
)
from app.core.llm import LLMService, resolve_openai_base_url
from app.core.embeddings import EmbeddingService, resolve_embedding_base_url
from app.core import vectorstore as vs_module


# ---------------- 配置与 provider 校验 ----------------

class TestProviderValidation:
    def test_valid_llm_providers_accept(self):
        for p in VALID_LLM_PROVIDERS:
            validate_provider(p, VALID_LLM_PROVIDERS, "LLM")  # 不应抛

    def test_invalid_provider_raises(self):
        with pytest.raises(ValueError):
            validate_provider("nvidia", VALID_LLM_PROVIDERS, "LLM")

    def test_llm_service_rejects_invalid(self, monkeypatch):
        monkeypatch.setattr(settings.llm, "provider", "nvidia")
        with pytest.raises(ValueError):
            LLMService()

    def test_embedding_service_rejects_invalid(self, monkeypatch):
        monkeypatch.setattr(settings.embedding, "provider", "nvidia")
        with pytest.raises(ValueError):
            EmbeddingService()


# ---------------- 国产 provider OpenAI 兼容路由 ----------------

class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeHTTPX:
    """替换 httpx.AsyncClient：记录调用并返回固定响应"""

    def __init__(self, data):
        self._data = data
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, headers=None, json=None):
        self.calls.append(("post", url, headers or {}, json or {}))
        return FakeResponse(self._data)

    async def get(self, url, headers=None):
        self.calls.append(("get", url, headers or {}))
        return FakeResponse({})


@pytest.mark.parametrize("provider,field", [
    ("ascend", "ascend_base_url"),
    ("cambricon", "cambricon_base_url"),
    ("mthreads", "mthreads_base_url"),
])
class TestDomesticProviderRouting:
    def test_resolve_base_url(self, monkeypatch, provider, field):
        monkeypatch.setattr(settings.llm, "provider", provider)
        monkeypatch.setattr(settings.llm, field, "http://10.0.0.5:8080/v1")
        monkeypatch.setattr(settings.llm, "openai_base_url", "https://api.openai.com/v1")
        assert resolve_openai_base_url() == "http://10.0.0.5:8080/v1"

    def test_fallback_to_openai_url(self, monkeypatch, provider, field):
        monkeypatch.setattr(settings.llm, "provider", provider)
        monkeypatch.setattr(settings.llm, field, "")  # 未配置国产地址
        monkeypatch.setattr(settings.llm, "openai_base_url", "https://api.openai.com/v1")
        assert resolve_openai_base_url() == "https://api.openai.com/v1"

    def test_chat_routes_to_domestic_url(self, monkeypatch, provider, field):
        """国产 provider 对话请求打到各自 base_url，且无 api_key 时不带 Authorization"""
        monkeypatch.setattr(settings.llm, "provider", provider)
        monkeypatch.setattr(settings.llm, field, "http://10.0.0.5:8080/v1")
        monkeypatch.setattr(settings.llm, "openai_api_key", "")
        fake = FakeHTTPX({"choices": [{"message": {"content": "你好"}}]})
        monkeypatch.setattr("httpx.AsyncClient", fake)
        svc = LLMService()
        import asyncio
        text = asyncio.run(svc.chat([{"role": "user", "content": "hi"}]))
        assert text == "你好"
        _, url, headers, _ = fake.calls[0]
        assert url == "http://10.0.0.5:8080/v1/chat/completions"
        assert "Authorization" not in headers

    def test_chat_tools_routes_and_key(self, monkeypatch, provider, field):
        """带工具调用 + 配置了 api_key 时带 Bearer 头"""
        monkeypatch.setattr(settings.llm, "provider", provider)
        monkeypatch.setattr(settings.llm, field, "http://10.0.0.5:8080/v1")
        monkeypatch.setattr(settings.llm, "openai_api_key", "sk-domestic")
        fake = FakeHTTPX({"choices": [{"message": {"content": "", "tool_calls": [
            {"function": {"name": "search_kb", "arguments": {"q": "x"}}}]}}]})
        monkeypatch.setattr("httpx.AsyncClient", fake)
        svc = LLMService()
        import asyncio
        resp = asyncio.run(svc.chat_with_tools([{"role": "user", "content": "hi"}], [{"type": "function"}]))
        assert resp.has_tool_calls and resp.tool_calls[0].name == "search_kb"
        _, url, headers, _ = fake.calls[0]
        assert url == "http://10.0.0.5:8080/v1/chat/completions"
        assert headers.get("Authorization") == "Bearer sk-domestic"

    def test_embedding_routes_to_domestic_url(self, monkeypatch, provider, field):
        monkeypatch.setattr(settings.embedding, "provider", provider)
        monkeypatch.setattr(settings.embedding, field, "http://10.0.0.5:8080/v1")
        monkeypatch.setattr(settings.embedding, "openai_api_key", "")
        fake = FakeHTTPX({"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]})
        monkeypatch.setattr("httpx.AsyncClient", fake)
        svc = EmbeddingService()
        import asyncio
        vecs = asyncio.run(svc.embed_text(["a", "b"]))
        assert vecs == [[0.1, 0.2], [0.3, 0.4]]
        _, url, headers, _ = fake.calls[0]
        assert url == "http://10.0.0.5:8080/v1/embeddings"

    def test_embedding_resolve_base_url(self, monkeypatch, provider, field):
        monkeypatch.setattr(settings.embedding, "provider", provider)
        monkeypatch.setattr(settings.embedding, field, "http://10.0.0.5:8080/v1")
        monkeypatch.setattr(settings.embedding, "openai_base_url", "https://api.openai.com/v1")
        assert resolve_embedding_base_url() == "http://10.0.0.5:8080/v1"


# ---------------- Milvus 后端 ----------------

class FakeMilvusClient:
    """模拟 pymilvus.MilvusClient（3.x）：内存集合，记录 create_collection 参数"""

    def __init__(self, uri=None, token=None):
        self.uri = uri
        self.token = token
        self.collections = {}
        self.created_kwargs = None

    def has_collection(self, name):
        return name in self.collections

    def create_collection(self, **kwargs):
        self.created_kwargs = kwargs
        self.collections[kwargs["collection_name"]] = []

    def insert(self, name, rows):
        self.collections.setdefault(name, []).extend(rows)

    def search(self, collection_name, data, limit, output_fields, search_params):
        rows = self.collections[collection_name]
        hits = []
        for r in rows[:limit]:
            hits.append({
                "id": r["id"],
                "distance": 0.12,
                "entity": {"content": r["content"], "metadata": r["metadata"]},
            })
        return [hits]

    def get_collection_stats(self, name):
        return {"row_count": len(self.collections[name])}

    def drop_collection(self, name):
        del self.collections[name]

    def list_collections(self):
        return list(self.collections)


@pytest.fixture
def fake_milvus(monkeypatch):
    fake = FakeMilvusClient()
    monkeypatch.setattr("pymilvus.MilvusClient", lambda uri=None, token=None: fake)
    monkeypatch.setattr(settings.vectorstore, "type", "milvus")
    monkeypatch.setattr(settings.vectorstore, "milvus_uri", "http://fake:19530")
    monkeypatch.setattr(settings.vectorstore, "milvus_token", "")
    monkeypatch.setattr(settings.vectorstore, "dimension", 8)
    return fake


class TestMilvusVectorStore:
    def test_create_collection_params(self, fake_milvus):
        vs = vs_module.MilvusVectorStore(collection_name="kb1")
        kw = fake_milvus.created_kwargs
        assert kw["collection_name"] == "kb1"
        assert kw["dimension"] == 8
        assert kw["metric_type"] == "COSINE"
        assert kw["id_type"] == "string"
        assert fake_milvus.token is None  # token 空串转 None

    def test_add_documents_serializes_metadata(self, fake_milvus):
        vs = vs_module.MilvusVectorStore(collection_name="kb1")
        vs.add_documents(
            ids=["a1", "a2"],
            documents=["文档一", "文档二"],
            embeddings=[[0.1] * 8, [0.2] * 8],
            metadatas=[{"filename": "f1.pdf", "page": 1}, {"filename": "f2.pdf"}],
        )
        rows = fake_milvus.collections["kb1"]
        assert len(rows) == 2
        assert rows[0]["id"] == "a1"
        assert rows[0]["content"] == "文档一"
        assert json.loads(rows[0]["metadata"]) == {"filename": "f1.pdf", "page": 1}

    def test_search_result_conversion(self, fake_milvus):
        vs = vs_module.MilvusVectorStore(collection_name="kb1")
        vs.add_documents(
            ids=["a1"], documents=["文档一"],
            embeddings=[[0.1] * 8],
            metadatas=[{"filename": "f1.pdf"}],
        )
        docs = vs.search([0.1] * 8, top_k=5)
        assert len(docs) == 1
        assert docs[0]["content"] == "文档一"
        assert docs[0]["metadata"] == {"filename": "f1.pdf"}
        assert abs(docs[0]["distance"] - 0.12) < 1e-9
        assert abs(docs[0]["score"] - 0.88) < 1e-9

    def test_count_delete_list(self, fake_milvus):
        vs = vs_module.MilvusVectorStore(collection_name="kb1")
        vs.add_documents(["a1"], ["d"], [[0.1] * 8], [{"k": 1}])
        assert vs.count() == 1
        assert vs.list_collections() == ["kb1"]
        vs.delete_collection()
        assert vs.list_collections() == []


# ---------------- 向量库工厂 ----------------

class TestVectorStoreFactory:
    def test_factory_chroma(self, monkeypatch):
        monkeypatch.setattr(settings.vectorstore, "type", "chroma")
        store = vs_module.get_vector_store("kb-x")
        assert isinstance(store, vs_module.ChromaVectorStore)

    def test_factory_milvus(self, fake_milvus):
        store = vs_module.get_vector_store("kb-y")
        assert isinstance(store, vs_module.MilvusVectorStore)

    def test_factory_invalid_type(self, monkeypatch):
        monkeypatch.setattr(settings.vectorstore, "type", "pgvector")
        with pytest.raises(ValueError):
            vs_module.get_vector_store("kb-z")

    def test_factory_routes_by_config(self, monkeypatch):
        """同一 collection 名，type 切换后返回不同后端"""
        monkeypatch.setattr(settings.vectorstore, "type", "chroma")
        assert isinstance(vs_module.get_vector_store("kb-r"), vs_module.ChromaVectorStore)
        fake = FakeMilvusClient()
        monkeypatch.setattr("pymilvus.MilvusClient", lambda uri=None, token=None: fake)
        monkeypatch.setattr(settings.vectorstore, "type", "milvus")
        assert isinstance(vs_module.get_vector_store("kb-r"), vs_module.MilvusVectorStore)

    def test_get_or_create_store_alias(self, monkeypatch):
        monkeypatch.setattr(settings.vectorstore, "type", "chroma")
        assert isinstance(vs_module.get_or_create_store("kb-a"), vs_module.ChromaVectorStore)

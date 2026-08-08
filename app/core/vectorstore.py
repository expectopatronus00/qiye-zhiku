"""向量存储模块 - ChromaDB（默认）与 Milvus（信创/大规模场景）双后端

v1.3 信创适配：新增 get_vector_store() 工厂，按 settings.vectorstore.type 路由
（chroma / milvus），所有调用方统一走工厂，业务层无感知切换后端。
Milvus 采用 pymilvus MilvusClient（3.x，兼容 2.4+ 服务端），
metadata 以 JSON 字符串存储（Milvus 不支持嵌套 dict 字段），返回时解析为 dict。
"""
import json
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.core.config import settings, validate_provider, VALID_VECTORSTORE_TYPES


class BaseVectorStore:
    """向量存储协议基类：各后端实现统一接口，供检索/文档/知识库等业务层使用"""

    collection_name: str

    def add_documents(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
    ):
        """添加文档到向量存储"""
        raise NotImplementedError

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        """向量相似度搜索，返回 [{content, metadata, distance, score}]"""
        raise NotImplementedError

    def delete_collection(self):
        """删除当前集合"""
        raise NotImplementedError

    def count(self) -> int:
        """返回集合中的文档数量"""
        raise NotImplementedError

    def list_collections(self) -> list[str]:
        """列出所有集合"""
        raise NotImplementedError


class ChromaVectorStore(BaseVectorStore):
    """ChromaDB 向量存储（默认后端）"""

    def __init__(self, collection_name: str = "default"):
        self.collection_name = collection_name
        persist_dir = Path(settings.vectorstore.persist_directory)
        persist_dir.mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def add_documents(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
    ):
        """添加文档到向量存储"""
        self.collection.add(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[dict]:
        """向量相似度搜索"""
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        docs = []
        if results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                docs.append({
                    "content": doc,
                    "metadata": results["metadatas"][0][i],
                    "distance": results["distances"][0][i],
                    "score": 1 - results["distances"][0][i],  # cosine distance -> similarity
                })
        return docs

    def delete_collection(self):
        """删除当前集合"""
        self.client.delete_collection(self.collection_name)

    def count(self) -> int:
        """返回集合中的文档数量"""
        return self.collection.count()

    def list_collections(self) -> list[str]:
        """列出所有集合"""
        collections = self.client.list_collections()
        return [c.name for c in collections]


class MilvusVectorStore(BaseVectorStore):
    """Milvus 向量存储（信创/大规模场景后端）

    依赖 pymilvus（延迟导入，未安装时不影响 Chroma 路径）；
    集合自动创建：COSINE 度量、字符串主键（128 位）、维度取 settings.vectorstore.dimension。
    """

    def __init__(self, collection_name: str = "default"):
        self.collection_name = collection_name
        from pymilvus import MilvusClient

        self.client = MilvusClient(
            uri=settings.vectorstore.milvus_uri,
            token=settings.vectorstore.milvus_token or None,
        )
        if not self.client.has_collection(collection_name):
            self.client.create_collection(
                collection_name=collection_name,
                dimension=settings.vectorstore.dimension,
                metric_type="COSINE",
                id_type="string",
                max_length=128,
            )

    def add_documents(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
    ):
        """添加文档到向量存储（metadata JSON 序列化落库）"""
        rows = [
            {
                "id": i,
                "content": doc,
                "embedding": emb,
                "metadata": json.dumps(md, ensure_ascii=False),
            }
            for i, doc, emb, md in zip(ids, documents, embeddings, metadatas)
        ]
        self.client.insert(self.collection_name, rows)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[dict]:
        """向量相似度搜索（COSINE，distance 越小越相似）"""
        results = self.client.search(
            collection_name=self.collection_name,
            data=[query_embedding],
            limit=top_k,
            output_fields=["content", "metadata"],
            search_params={"metric_type": "COSINE", "params": {"nprobe": 10}},
        )
        docs = []
        for hit in (results[0] if results else []):
            entity = hit.get("entity", {})
            try:
                metadata = json.loads(entity.get("metadata") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            distance = hit.get("distance", 1.0)
            docs.append({
                "content": entity.get("content", ""),
                "metadata": metadata,
                "distance": distance,
                "score": 1 - distance,
            })
        return docs

    def delete_collection(self):
        """删除当前集合"""
        self.client.drop_collection(self.collection_name)

    def count(self) -> int:
        """返回集合中的文档数量"""
        stats = self.client.get_collection_stats(self.collection_name)
        return int(stats.get("row_count", 0))

    def list_collections(self) -> list[str]:
        """列出所有集合"""
        return list(self.client.list_collections())


# 兼容别名：Chroma 为默认后端，旧调用方 import VectorStore 语义不变
VectorStore = ChromaVectorStore


def get_vector_store(collection_name: str = "default") -> BaseVectorStore:
    """向量存储工厂（v1.3）：按 settings.vectorstore.type 路由 chroma / milvus"""
    vtype = settings.vectorstore.type
    if vtype == "chroma":
        return ChromaVectorStore(collection_name=collection_name)
    if vtype == "milvus":
        return MilvusVectorStore(collection_name=collection_name)
    raise ValueError(f"不支持的向量库类型: {vtype}，可选: {sorted(VALID_VECTORSTORE_TYPES)}")


def get_or_create_store(collection_name: str) -> BaseVectorStore:
    """兼容旧静态方法名：等价于 get_vector_store"""
    return get_vector_store(collection_name=collection_name)

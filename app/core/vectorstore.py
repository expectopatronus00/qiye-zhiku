"""向量存储模块 - 基于 ChromaDB"""
from pathlib import Path
from typing import Optional
import chromadb
from chromadb.config import Settings as ChromaSettings
from app.core.config import settings


class VectorStore:
    """ChromaDB 向量存储"""

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

    @staticmethod
    def get_or_create_store(collection_name: str) -> "VectorStore":
        """获取或创建向量存储"""
        return VectorStore(collection_name=collection_name)

"""知识库管理 API 路由"""
from fastapi import APIRouter
from app.core.vectorstore import VectorStore

router = APIRouter()


@router.get("/collections")
async def list_collections():
    """列出所有知识库"""
    store = VectorStore()
    collections = store.list_collections()
    result = []
    for name in collections:
        vs = VectorStore(collection_name=name)
        result.append({
            "name": name,
            "chunk_count": vs.count(),
        })
    return {"collections": result}


@router.post("/collections/{name}")
async def create_collection(name: str):
    """创建新知识库"""
    store = VectorStore(collection_name=name)
    return {
        "name": name,
        "chunk_count": store.count(),
        "status": "created",
    }

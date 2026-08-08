"""知识库管理 API 路由 (v0.7 支持权限隔离)"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import (
    User,
    get_audit_logger,
    get_current_user,
    get_kb_registry,
    require_admin,
)
from app.core.vectorstore import get_vector_store

router = APIRouter()


class CollectionInfo(BaseModel):
    name: str
    chunk_count: int
    owner: str
    display_name: str
    created_at: str


@router.get("/collections")
async def list_collections(user: User = Depends(get_current_user)):
    """列出当前用户可见的知识库（管理员可见全部）"""
    registry = get_kb_registry()
    result = []
    for kb in registry.list_for(user):
        vs = get_vector_store(collection_name=kb.name)
        result.append({
            "name": kb.name,
            "chunk_count": vs.count(),
            "owner": kb.owner,
            "display_name": kb.display_name,
            "created_at": kb.created_at,
        })
    return {"collections": result}


@router.post("/collections/{name}", response_model=CollectionInfo)
async def create_collection(name: str, user: User = Depends(get_current_user)):
    """创建新知识库（登记属主，仅属主/管理员可用）"""
    registry = get_kb_registry()
    try:
        kb = registry.create(name, user.username, display_name=name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 真正创建 Chroma collection（惰性创建，确保计数可用）
    get_vector_store(collection_name=name)
    get_audit_logger().log(user.username, "kb.create", name, "创建知识库")
    return CollectionInfo(
        name=kb.name, chunk_count=0,
        owner=kb.owner, display_name=kb.display_name, created_at=kb.created_at,
    )


@router.delete("/collections/{name}")
async def delete_collection(name: str, user: User = Depends(get_current_user)):
    """删除知识库（仅属主或管理员）"""
    registry = get_kb_registry()
    kb = registry.get(name)
    if kb is None:
        raise HTTPException(status_code=404, detail=f"知识库 '{name}' 不存在")
    if not user.is_admin and kb.owner != user.username:
        raise HTTPException(status_code=403, detail=f"无权删除知识库 '{name}'（仅属主或管理员可用）")
    try:
        get_vector_store(collection_name=name).delete_collection()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")
    registry.delete(name)
    get_audit_logger().log(user.username, "kb.delete", name, "删除知识库")
    return {"status": "success", "message": f"知识库 '{name}' 已删除"}


@router.get("/collections/{name}/access")
async def check_access(name: str, user: User = Depends(get_current_user)):
    """查询当前用户对指定知识库的访问权限"""
    registry = get_kb_registry()
    kb = registry.get(name)
    if kb is None:
        raise HTTPException(status_code=404, detail=f"知识库 '{name}' 不存在")
    return {
        "name": name,
        "owner": kb.owner,
        "accessible": registry.can_access(name, user),
        "is_owner": kb.owner == user.username,
    }

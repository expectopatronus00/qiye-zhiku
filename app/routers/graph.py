"""知识图谱 API (v1.6 进阶能力)

- GET /api/graph/collections            有图谱的知识库列表
- GET /api/graph/entities/{collection}  实体列表（按出现次数排序）
- GET /api/graph/relations/{collection}?entity=xxx  某实体的关系（含权重/方向）
- GET /api/graph/stats/{collection}     图谱统计
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import settings
from app.core.graph import graph_builder
from app.core.security import User, get_current_user, get_kb_registry, require_kb_access

router = APIRouter()


@router.get("/collections")
async def list_graph_collections(user: User = Depends(get_current_user)):
    """有图谱的知识库列表（仅当前用户可见的库）"""
    registry = get_kb_registry()
    visible = {kb.name for kb in registry.list_for(user)}
    return [c for c in graph_builder.all_collections() if c in visible]


@router.get("/entities/{collection}")
async def list_entities(
    collection: str,
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user),
):
    """实体列表（需知识库访问权限）"""
    require_kb_access(collection, user)
    return {"collection": collection, "items": graph_builder.entities(collection, limit)}


@router.get("/relations/{collection}")
async def list_relations(
    collection: str,
    entity: str = Query(..., description="实体名"),
    limit: int = Query(30, ge=1, le=200),
    user: User = Depends(get_current_user),
):
    """实体的关系列表（需知识库访问权限）"""
    require_kb_access(collection, user)
    rels = graph_builder.relations(collection, entity, limit)
    return {"collection": collection, "entity": entity, "items": rels}


@router.get("/stats/{collection}")
async def graph_stats(collection: str, user: User = Depends(get_current_user)):
    """图谱统计（需知识库访问权限）"""
    require_kb_access(collection, user)
    return {"collection": collection, **graph_builder.stats(collection)}

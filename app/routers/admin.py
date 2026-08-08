"""系统管理后台 API 路由 (v1.1) - 仅管理员可访问

- 用户管理: 列表/代建/启用禁用/角色/重置密码/删除/解锁
- 知识库管理: 全部库用量统计 + 配额设置
- 系统配置: 查看(脱敏)/热更新(写回 config.yaml)
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.config import get_config_view, update_config
from app.core.security import (
    User,
    get_audit_logger,
    get_feedback_manager,
    get_kb_registry,
    get_user_manager,
    require_admin,
)
from app.core.vectorstore import get_vector_store

router = APIRouter()


# ---------------- 用户管理 ----------------

class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    display_name: str = ""
    role: str = "user"


class UserPatch(BaseModel):
    display_name: str | None = None
    role: str | None = None
    enabled: bool | None = None


class ResetPassword(BaseModel):
    new_password: str = Field(min_length=6, max_length=64)


@router.get("/users")
async def list_users(
    keyword: str = "",
    page: int = 1,
    size: int = 20,
    admin: User = Depends(require_admin),
):
    """用户列表（不含密码哈希/令牌）"""
    return get_user_manager().list_users(
        keyword=keyword, page=max(1, page), size=min(max(1, size), 100))


@router.post("/users")
async def create_user(body: UserCreate, admin: User = Depends(require_admin)):
    """管理员代建用户"""
    um = get_user_manager()
    try:
        user = um.admin_create_user(
            body.username, body.password, body.display_name, body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    get_audit_logger().log(admin.username, "admin.user_create", user.username,
                           f"代建用户，角色 {user.role}")
    return {"username": user.username, "role": user.role,
            "display_name": user.display_name, "enabled": True}


@router.patch("/users/{username}")
async def patch_user(username: str, body: UserPatch,
                     admin: User = Depends(require_admin)):
    """更新用户：显示名/角色/启用禁用"""
    if username.lower() == get_user_manager().admin_username.lower() and \
            body.enabled is False:
        raise HTTPException(status_code=400, detail="不能禁用系统管理员")
    if body.role and body.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="角色仅支持 user / admin")
    um = get_user_manager()
    if not um.get_user(username):
        raise HTTPException(status_code=404, detail=f"用户 '{username}' 不存在")
    try:
        if body.display_name is not None:
            um.set_display_name(username, body.display_name)
        if body.role is not None:
            um.set_role(username, body.role)
        if body.enabled is not None:
            um.set_enabled(username, body.enabled)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    action = "admin.user_disable" if body.enabled is False else "admin.user_update"
    get_audit_logger().log(admin.username, action, username,
                           f"角色={body.role or '-'} 启用={body.enabled if body.enabled is not None else '-'}")
    return {"status": "success", "user": um.get_user(username)}


@router.delete("/users/{username}")
async def delete_user(username: str, admin: User = Depends(require_admin)):
    """删除用户；其名下知识库自动转移给当前管理员（数据不丢失）"""
    um = get_user_manager()
    if username == admin.username:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    if username.lower() == um.admin_username.lower():
        raise HTTPException(status_code=400, detail="不能删除系统管理员")
    if not um.get_user(username):
        raise HTTPException(status_code=404, detail=f"用户 '{username}' 不存在")
    transferred = get_kb_registry().transfer_owner(username, admin.username)
    um.delete_user(username)
    get_audit_logger().log(admin.username, "admin.user_delete", username,
                           f"删除用户，转移知识库 {transferred} 个")
    return {"status": "success", "transferred_kbs": transferred}


@router.post("/users/{username}/reset-password")
async def reset_password(username: str, body: ResetPassword,
                         admin: User = Depends(require_admin)):
    """管理员重置密码（吊销该用户全部令牌）"""
    um = get_user_manager()
    try:
        um.reset_password(username, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    get_audit_logger().log(admin.username, "admin.user_reset_password", username)
    return {"status": "success", "message": f"已重置用户 '{username}' 的密码"}


@router.post("/users/{username}/unlock")
async def unlock_user(username: str, admin: User = Depends(require_admin)):
    """解锁被连续失败锁定的账号"""
    um = get_user_manager()
    try:
        um.unlock(username)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    get_audit_logger().log(admin.username, "admin.user_unlock", username)
    return {"status": "success", "message": f"已解锁用户 '{username}'"}


# ---------------- 知识库配额 ----------------

class QuotaPatch(BaseModel):
    quota_chunks: int | None = None          # -1 不限制
    quota_documents: int | None = None       # -1 不限制


def _kb_usage(name: str) -> dict:
    """知识库用量：块数 + 文档数（元数据 filename 去重）"""
    vs = get_vector_store(collection_name=name)
    chunk_count = vs.count()
    doc_count = 0
    try:
        metas = vs.get_metadatas()
        doc_count = len({m.get("filename", "unknown") for m in metas})
    except Exception:
        pass
    return {"chunk_count": chunk_count, "document_count": doc_count}


@router.get("/knowledge-bases")
async def list_knowledge_bases(admin: User = Depends(require_admin)):
    """全部知识库 + 用量 + 配额"""
    result = []
    for kb in get_kb_registry().list_all():
        usage = _kb_usage(kb.name)
        result.append({
            "name": kb.name,
            "owner": kb.owner,
            "display_name": kb.display_name,
            "created_at": kb.created_at,
            **usage,
            "quota_chunks": kb.quota_chunks,
            "quota_documents": kb.quota_documents,
        })
    return {"collections": result}


@router.patch("/knowledge-bases/{name}")
async def patch_kb_quota(name: str, body: QuotaPatch,
                         admin: User = Depends(require_admin)):
    """设置知识库配额（配额不可低于当前用量）"""
    registry = get_kb_registry()
    kb = registry.get(name)
    if kb is None:
        raise HTTPException(status_code=404, detail=f"知识库 '{name}' 不存在")
    usage = _kb_usage(name)
    new_chunks = body.quota_chunks if body.quota_chunks is not None else kb.quota_chunks
    new_docs = body.quota_documents if body.quota_documents is not None else kb.quota_documents
    if new_chunks != -1 and new_chunks < usage["chunk_count"]:
        raise HTTPException(
            status_code=400,
            detail=f"块数配额({new_chunks})低于当前用量({usage['chunk_count']})，请先清理文档")
    if new_docs != -1 and new_docs < usage["document_count"]:
        raise HTTPException(
            status_code=400,
            detail=f"文档数配额({new_docs})低于当前用量({usage['document_count']})，请先清理文档")
    try:
        registry.set_quota(name, new_chunks, new_docs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    get_audit_logger().log(admin.username, "kb.quota", name,
                           f"块配额={new_chunks} 文档配额={new_docs}")
    return {"status": "success", "quota_chunks": new_chunks,
            "quota_documents": new_docs}


# ---------------- 系统配置 ----------------

@router.get("/config")
async def get_config(admin: User = Depends(require_admin)):
    """读取可编辑配置（密钥脱敏）"""
    return {"config": get_config_view()}


@router.patch("/config")
async def patch_config(request: Request, admin: User = Depends(require_admin)):
    """热更新配置并写回 config.yaml（仅白名单字段）"""
    patch = await request.json()
    try:
        view = update_config(patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    changed = {k: patch[k] for k in patch}
    get_audit_logger().log(admin.username, "config.update", "",
                           f"更新配置节: {','.join(changed)}")
    return {"status": "success", "config": view}


# ---------------- 用户反馈 (v1.2) ----------------

@router.get("/feedback")
async def list_feedback(
    rating: str = "",
    limit: int = 200,
    admin: User = Depends(require_admin),
):
    """反馈列表（可按 rating 筛选）"""
    rows = get_feedback_manager().list(
        rating=rating if rating in ("up", "down") else None,
        limit=min(limit, 500),
    )
    return {"total": get_feedback_manager().count(), "items": rows}


@router.get("/feedback/export")
async def export_feedback_dataset(admin: User = Depends(require_admin)):
    """回流黄金评测集：点踩 + 期望回答的反馈 → 评测集 JSON"""
    data = get_feedback_manager().export_dataset()
    get_audit_logger().log(admin.username, "feedback.export", "",
                           f"回流评测集 {len(data['items'])} 条")
    return data

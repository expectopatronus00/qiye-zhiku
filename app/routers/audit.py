"""操作审计日志 API 路由 (v0.7) - 仅管理员可查询"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.security import User, get_audit_logger, require_admin

router = APIRouter()


class AuditItem(BaseModel):
    id: int
    ts: str
    user: str
    action: str
    target: str
    detail: str


class AuditPage(BaseModel):
    total: int
    page: int
    size: int
    items: list[AuditItem]


@router.get("", response_model=AuditPage)
async def list_audit(
    user: str = "",
    action: str = "",
    page: int = 1,
    size: int = 50,
    admin: User = Depends(require_admin),
):
    """查询审计日志（admin 专属，支持按用户/动作过滤 + 分页）"""
    page = max(1, page)
    size = min(max(1, size), 200)
    data = get_audit_logger().query(user_filter=user, action_filter=action,
                                    page=page, size=size)
    return AuditPage(
        total=data["total"], page=data["page"], size=data["size"],
        items=[AuditItem(**item) for item in data["items"]],
    )

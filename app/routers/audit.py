"""操作审计日志 API 路由 (v0.7 查询 / v1.1 导出) - 仅管理员可查询"""
import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import Response
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


@router.get("/export")
async def export_audit(
    user: str = "",
    action: str = "",
    admin: User = Depends(require_admin),
):
    """导出审计日志为 CSV（UTF-8 BOM，Excel 直接打开不乱码；最多 5000 条）"""
    data = get_audit_logger().query(user_filter=user, action_filter=action,
                                    page=1, size=5000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "时间", "用户", "动作", "对象", "详情"])
    for item in data["items"]:
        writer.writerow([item["id"], item["ts"], item["user"],
                         item["action"], item["target"], item["detail"]])
    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    filename = f"audit_log_{user or 'all'}_{action or 'all'}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

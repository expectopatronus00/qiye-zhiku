"""任务状态查询 API (v1.5 性能与高可用)

- GET /api/tasks/{task_id}  查询单个任务状态
- GET /api/tasks            任务列表（分页 + 按状态/创建人过滤）
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import User, get_current_user
from app.core.tasks import task_manager

router = APIRouter()


@router.get("/{task_id}")
async def get_task(task_id: str, user: User = Depends(get_current_user)):
    """查询任务状态（需登录）"""
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("")
async def list_tasks(
    status: str = Query("", description="按状态过滤: pending/running/success/failed"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
):
    """任务列表（仅本人任务；管理员可见全部）"""
    created_by = "" if user.is_admin else user.username
    return task_manager.list(created_by=created_by, status=status, page=page, size=size)

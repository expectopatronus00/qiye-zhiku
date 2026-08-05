"""用户认证 API 路由 (v0.7)"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.core.security import (
    User,
    _bearer,
    get_audit_logger,
    get_current_user,
    get_user_manager,
)

router = APIRouter()


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, description="用户名（2-32位，支持中文/字母/数字/下划线）")
    password: str = Field(..., min_length=6, max_length=64, description="密码（至少6位）")
    display_name: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    username: str
    role: str
    display_name: str
    is_admin: bool


class LoginResponse(BaseModel):
    token: str
    user: UserResponse


@router.post("/register", response_model=UserResponse)
async def register(req: RegisterRequest, admin: User = Depends(get_current_user)):
    """注册新用户（仅管理员可注册）"""
    if not admin.is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可注册用户")
    try:
        user = get_user_manager().register(req.username, req.password, req.display_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    get_audit_logger().log(admin.username, "user.register", req.username,
                           f"管理员 {admin.username} 注册用户 {req.username}")
    return UserResponse(username=user.username, role=user.role,
                        display_name=user.display_name, is_admin=user.is_admin)


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """登录，返回不透明令牌（24h 有效）"""
    user, token = get_user_manager().login(req.username, req.password)
    if user is None:
        get_audit_logger().log(req.username, "auth.login_failed", req.username,
                               f"登录失败: {token}")
        raise HTTPException(status_code=401, detail=token)
    get_audit_logger().log(user.username, "auth.login", "", "登录成功")
    return LoginResponse(token=token, user=UserResponse(
        username=user.username, role=user.role,
        display_name=user.display_name, is_admin=user.is_admin))


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)):
    """当前登录用户信息"""
    return UserResponse(username=user.username, role=user.role,
                        display_name=user.display_name, is_admin=user.is_admin)


@router.post("/logout")
async def logout(user: User = Depends(get_current_user),
                 credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """登出（清除令牌）"""
    get_audit_logger().log(user.username, "auth.logout", "", "登出")
    if credentials:
        get_user_manager().logout(credentials.credentials)
    return {"status": "ok"}


@router.post("/change-password")
async def change_password(req: dict, user: User = Depends(get_current_user)):
    """修改自己的密码"""
    old_password = (req.get("old_password") or "").strip()
    new_password = (req.get("new_password") or "").strip()
    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="请填写原密码和新密码")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度至少 6 位")
    ok, msg = get_user_manager().change_password(user.username, old_password, new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    get_audit_logger().log(user.username, "auth.change_password", "", "修改密码")
    return {"status": "ok"}

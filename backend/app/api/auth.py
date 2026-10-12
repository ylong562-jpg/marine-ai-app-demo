"""
Authentication API endpoints.
"""
import os
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.schemas import UserCreate, UserLogin, TokenResponse, UserInfo, RegisterResponse
from app.services.auth_service import (
    authenticate_user,
    create_access_token,
    create_user,
    get_user_by_username,
)
from app.core.security import get_current_user
from app.core.config import get_settings
from app.db.models import User

router = APIRouter(prefix="/auth", tags=["Auth"])
settings = get_settings()


def _is_core(user: User) -> bool:
    """Core admins are defined by env ADMIN_USERS (e.g. admin)."""
    admin_users = os.getenv("ADMIN_USERS", "admin").split(",")
    return user.username in admin_users


def _is_admin(user: User) -> bool:
    """Admins include core admins and users with db is_admin=True."""
    return _is_core(user) or bool(user.is_admin)


def _is_project_manager(user: User) -> bool:
    """任务管理权限：仅被授予 is_project_manager 的用户（管理员不再天然具备）。

    与 _is_admin 明确分离：系统管理员只管用户/授权等系统事务，
    任务模块（发布/管理任务、项目追踪、邮箱维护、手动同步）只认任务管理员。
    """
    return bool(getattr(user, "is_project_manager", False))


@router.post("/register", response_model=RegisterResponse)
async def register(req: UserCreate, db: AsyncSession = Depends(get_db)):
    if not settings.ALLOW_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="注册已关闭，请联系管理员",
        )
    existing = await get_user_by_username(db, req.username)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在",
        )
    user = await create_user(db, req.username, req.password, is_approved=False)
    return RegisterResponse(
        status="pending",
        message="注册申请已提交，请等待管理员审核通过后再登录",
        username=user.username,
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: UserLogin, db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, req.username, req.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    if not user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号正在审核中，请等待管理员批准后再登录",
        )
    token = create_access_token({"sub": user.username})
    return TokenResponse(access_token=token, username=user.username)


@router.get("/me", response_model=UserInfo)
async def me(current_user: User = Depends(get_current_user)):
    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        is_active=current_user.is_active,
        is_approved=current_user.is_approved,
        is_admin=_is_admin(current_user),
        is_core=_is_core(current_user),
        is_project_manager=_is_project_manager(current_user),
    )

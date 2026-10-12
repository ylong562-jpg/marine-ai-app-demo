"""
Admin endpoints for user management.
Three-level permission system:
- Core admin (env ADMIN_USERS): can promote/demote admins, delete anyone except self, full access
- Admin (db is_admin=True): can approve/reject/delete normal users
- Normal user: no admin access
"""
import os
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import User
from app.models.schemas import PendingUser, UserActionResponse, AllUser
from app.core.security import get_current_user
from app.core.config import get_settings
from app.api.auth import _is_core, _is_admin

router = APIRouter(prefix="/admin", tags=["Admin"])
settings = get_settings()


async def get_current_core(
    current_user: User = Depends(get_current_user),
) -> User:
    """Only core admins can pass."""
    if not _is_core(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要核心管理员权限",
        )
    return current_user


async def get_current_manager(
    current_user: User = Depends(get_current_user),
) -> User:
    """Core admins and admins can pass."""
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


@router.get("/users", response_model=list[AllUser])
async def list_all_users(
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_manager),
):
    """List users. Core admins see everyone.
    Normal admins see only normal users (not core, not admins).
    """
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()

    admin_users_env = os.getenv("ADMIN_USERS", "admin").split(",")

    filtered = []
    for u in users:
        is_core = u.username in admin_users_env
        if _is_admin(manager) and not _is_core(manager):
            # Normal admin: can only see normal users
            if is_core or u.is_admin:
                continue
        filtered.append(u)

    return [
        AllUser(
            id=u.id,
            username=u.username,
            plain_password=u.plain_password if _is_core(manager) else None,
            is_active=u.is_active,
            is_approved=u.is_approved,
            is_admin=bool(u.is_admin),
            is_core=(u.username in admin_users_env),
            is_project_manager=bool(getattr(u, "is_project_manager", False)),
            created_at=u.created_at.isoformat() if u.created_at else "",
        )
        for u in filtered
    ]


@router.get("/users/pending", response_model=list[PendingUser])
async def list_pending_users(
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_manager),
):
    result = await db.execute(
        select(User)
        .where(User.is_approved == False, User.is_active == True)
        .order_by(User.created_at)
    )
    users = result.scalars().all()
    return [
        PendingUser(
            id=u.id,
            username=u.username,
            created_at=u.created_at.isoformat() if u.created_at else "",
        )
        for u in users
    ]


@router.post("/users/{user_id}/approve", response_model=UserActionResponse)
async def approve_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_manager),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if _is_core(user):
        raise HTTPException(status_code=403, detail="不能操作核心管理员账号")
    user.is_approved = True
    await db.commit()
    return UserActionResponse(
        status="approved",
        message=f"用户 {user.username} 已通过审核",
    )


@router.post("/users/{user_id}/reject", response_model=UserActionResponse)
async def reject_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_manager),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if _is_core(user):
        raise HTTPException(status_code=403, detail="不能操作核心管理员账号")
    user.is_active = False
    await db.commit()
    return UserActionResponse(
        status="rejected",
        message=f"用户 {user.username} 已被拒绝并禁用",
    )


@router.delete("/users/{user_id}", response_model=UserActionResponse)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_manager),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if _is_core(user):
        raise HTTPException(status_code=403, detail="不能删除核心管理员账号")
    if user.id == manager.id:
        raise HTTPException(status_code=403, detail="不能删除当前登录的账号")
    # Normal admin cannot delete other admins
    if not _is_core(manager) and user.is_admin:
        raise HTTPException(status_code=403, detail="普通管理员不能删除其他管理员")
    username = user.username
    await db.delete(user)
    await db.commit()
    return UserActionResponse(
        status="deleted",
        message=f"用户 {username} 已删除",
    )


@router.post("/users/{user_id}/promote", response_model=UserActionResponse)
async def promote_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    core: User = Depends(get_current_core),
):
    """Core admin only: promote a normal user to admin."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if _is_core(user):
        raise HTTPException(status_code=403, detail="核心管理员无需再提升")
    user.is_admin = True
    await db.commit()
    return UserActionResponse(
        status="promoted",
        message=f"用户 {user.username} 已被提升为管理员",
    )


@router.post("/users/{user_id}/demote", response_model=UserActionResponse)
async def demote_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    core: User = Depends(get_current_core),
):
    """Core admin only: demote an admin to normal user."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if _is_core(user):
        raise HTTPException(status_code=403, detail="不能降级核心管理员")
    if not user.is_admin:
        raise HTTPException(status_code=400, detail="该用户不是管理员")
    user.is_admin = False
    await db.commit()
    return UserActionResponse(
        status="demoted",
        message=f"用户 {user.username} 已被降级为普通用户",
    )


@router.post("/users/{user_id}/grant-project-manager", response_model=UserActionResponse)
async def grant_project_manager(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    core: User = Depends(get_current_core),
):
    """Core admin only: grant the independent '任务管理员' permission."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if _is_core(user):
        raise HTTPException(status_code=403, detail="核心管理员无需该权限")
    user.is_project_manager = True
    await db.commit()
    return UserActionResponse(
        status="granted",
        message=f"用户 {user.username} 已获得「任务管理员」权限",
    )


@router.post("/users/{user_id}/revoke-project-manager", response_model=UserActionResponse)
async def revoke_project_manager(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    core: User = Depends(get_current_core),
):
    """Core admin only: revoke the '任务管理员' permission."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if _is_core(user):
        raise HTTPException(status_code=403, detail="不能收回核心管理员的权限")
    if not user.is_project_manager:
        raise HTTPException(status_code=400, detail="该用户没有「任务管理员」权限")
    user.is_project_manager = False
    await db.commit()
    return UserActionResponse(
        status="revoked",
        message=f"用户 {user.username} 的「任务管理员」权限已收回",
    )

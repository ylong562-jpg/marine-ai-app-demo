# -*- coding: utf-8 -*-
"""
船厂交易同步 API

提供手动触发和状态查询接口，用于调试即将上线的定时任务。
"""
import importlib
import os
import sys
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.auth import _is_admin
from app.core.security import get_current_user
from app.db.models import User

router = APIRouter(prefix="/shipyard-transactions", tags=["Shipyard Transactions"])


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


class SyncRequest(BaseModel):
    start_date: Optional[str] = Field(default=None, description="开始日期 YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="结束日期 YYYY-MM-DD")
    dry_run: bool = Field(default=False, description="是否试运行，不写入数据库")


@router.post("/sync")
async def sync_shipyard_transactions_api(
    req: SyncRequest,
    current_user: User = Depends(_require_admin),
):
    """手动触发船厂交易同步任务。默认处理今天和昨天的新闻。"""
    try:
        scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        import sync_shipyard_transactions
        importlib.reload(sync_shipyard_transactions)

        result = sync_shipyard_transactions.sync_shipyard_transactions(
            start_date=req.start_date,
            end_date=req.end_date,
            dry_run=req.dry_run,
        )

        # 统一返回字段，方便前端展示
        return {
            "success": result.get("success", True),
            "dry_run": result.get("dry_run", req.dry_run),
            "date_range": result.get("date_range", [req.start_date, req.end_date]),
            "updated_shipyards": result.get("updated_shipyards", 0),
            "pending_transactions": result.get("pending_transactions", 0),
            "skipped_count": len(result.get("skipped", [])),
            "skipped": result.get("skipped", []),
            "pending_detail": result.get("pending_detail") or result.get("pending"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失败: {e}")


@router.get("/status")
async def get_sync_status(
    current_user: User = Depends(_require_admin),
):
    """返回同步任务状态（当前仅返回当前时间，可扩展为读取日志/DB 记录）。"""
    return {
        "success": True,
        "message": "船厂交易同步任务尚未启用定时运行，请通过 /sync 手动触发测试",
        "current_time": datetime.now().isoformat(),
    }

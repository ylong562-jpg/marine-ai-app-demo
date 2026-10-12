"""
数据导入 API（临时模块）

用于前端"数据导入"页面：将三段利率原文粘贴后一键导入 daily_data。
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Dict

from app.api.auth import _is_admin
from app.core.security import get_current_user
from app.db.models import User
from app.scripts.import_interest_rates import import_interest_rates

router = APIRouter(prefix="/data-import", tags=["Data Import"])


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


class ImportInterestRatesRequest(BaseModel):
    us_treasury: str = Field(
        default="",
        description="U.S. Treasury Yields 原始文本",
    )
    sofr_swaps: str = Field(
        default="",
        description="1-month Term SOFR Swaps 原始文本",
    )
    sofr_overnight: str = Field(
        default="",
        description="Secured Overnight Financing Rate (SOFR) 原始文本",
    )
    dry_run: bool = Field(
        default=False,
        description="为 True 时只解析并返回统计，不写入数据库",
    )


class ImportInterestRatesResponse(BaseModel):
    success: bool
    us_treasury: int
    sofr_swaps: int
    sofr_overnight: int
    message: str


@router.post(
    "/interest-rates",
    response_model=ImportInterestRatesResponse,
    summary="手动导入三类利率数据",
)
async def import_interest_rates_endpoint(
    req: ImportInterestRatesRequest,
    current_user: User = Depends(_require_admin),
):
    """接收三段利率原文，解析后写入 Time_Series.daily_data。"""
    try:
        result = import_interest_rates(
            {
                "us_treasury": req.us_treasury,
                "sofr_swaps": req.sofr_swaps,
                "sofr_overnight": req.sofr_overnight,
            },
            dry_run=req.dry_run,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导入失败: {e}",
        )
    return result

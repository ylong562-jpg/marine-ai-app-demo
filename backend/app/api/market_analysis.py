# -*- coding: utf-8 -*-
"""
市场分析 API

提供新造船市场、买卖船市场、租船市场的交易数据查询与聚合统计。
"""
import asyncio
import json
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.auth import _is_admin
from app.core.security import get_current_user
from app.db.models import User
from app.services.market_analysis_service import (
    aggregate_by_date,
    aggregate_by_ship_type,
    compute_ma,
    fetch_market_records,
    get_last_sync_time,
    update_market_record,
)

router = APIRouter(prefix="/market-analysis", tags=["Market Analysis"])

# 市场分析同步任务缓存（内存态，重启后丢失；如需持久化可改为 Redis/DB）
_sync_tasks: Dict[str, Dict[str, Any]] = {}
_sync_tasks_lock = asyncio.Lock()
_SYNC_TASK_MAX_AGE_SECONDS = 3600 * 24  # 保留 24 小时


async def _record_sync_task(task_id: str, status: str, result: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
    async with _sync_tasks_lock:
        _sync_tasks[task_id] = {
            "status": status,
            "result": result,
            "error": error,
            "updated_at": time.time(),
        }
        # 清理过期任务
        now = time.time()
        expired = [tid for tid, t in _sync_tasks.items() if now - t["updated_at"] > _SYNC_TASK_MAX_AGE_SECONDS]
        for tid in expired:
            _sync_tasks.pop(tid, None)


async def _run_sync_task(
    task_id: str,
    category: str,
    start_date: Optional[str],
    end_date: Optional[str],
    full_rebuild: bool,
) -> None:
    """在线程池中执行同步，避免阻塞事件循环。"""
    from app.services.market_analysis_sync_service import sync_market_transactions

    try:
        await _record_sync_task(task_id, "running")
        result = await asyncio.to_thread(
            lambda: asyncio.run(
                sync_market_transactions(
                    category=category,
                    start_date=start_date,
                    end_date=end_date,
                    full_rebuild=full_rebuild,
                )
            )
        )
        await _record_sync_task(task_id, "completed", result=result)
    except Exception as e:
        await _record_sync_task(task_id, "failed", error=str(e))


@lru_cache(maxsize=1)
def _load_ship_type_taxonomy() -> List[Dict[str, Any]]:
    """加载 news_engine 中的船型标准化词表。"""
    taxonomy_path = (
        Path(__file__).resolve().parent.parent
        / "news_engine"
        / "data"
        / "ship_type_taxonomy.json"
    )
    if not taxonomy_path.exists():
        return []
    with open(taxonomy_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("entries", [])


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


class MarketAnalysisRequest(BaseModel):
    category: str = Field(..., description="newbuilding | sale | charter")
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    keyword: Optional[str] = Field(default=None)
    ship_type: Optional[str] = Field(default=None)
    limit: int = Field(default=2000)


class MarketAnalysisSyncRequest(BaseModel):
    category: str = Field(default="all", description="all | newbuilding | sale | charter")
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    full_rebuild: bool = Field(default=False, description="是否重新处理全部数据")


class UpdateMarketRecordRequest(BaseModel):
    category: str = Field(..., description="newbuilding | sale | charter")
    date: Optional[str] = Field(default=None)
    ship_type: Optional[str] = Field(default=None)
    specs: Optional[str] = Field(default=None)
    quantity: Optional[str] = Field(default=None)
    original_price: Optional[str] = Field(default=None)
    cny_min: Optional[str] = Field(default=None)
    cny_max: Optional[str] = Field(default=None)
    trade_type: Optional[str] = Field(default=None)
    delivery: Optional[str] = Field(default=None)
    power: Optional[str] = Field(default=None)
    charter_type: Optional[str] = Field(default=None)
    shipowner: Optional[str] = Field(default=None)
    shipyard: Optional[str] = Field(default=None)
    buyer: Optional[str] = Field(default=None)
    seller: Optional[str] = Field(default=None)
    charterer: Optional[str] = Field(default=None)
    owner: Optional[str] = Field(default=None)


@router.post("/records")
async def get_market_records(
    req: MarketAnalysisRequest,
    current_user: User = Depends(_require_admin),
) -> Dict[str, Any]:
    """查询指定市场类别的原始交易记录。"""
    category_map = {"newbuilding": "新造船订单", "sale": "买卖船", "charter": "租船"}
    if req.category not in category_map:
        raise HTTPException(status_code=400, detail=f"不支持的市场类别: {req.category}")

    records = fetch_market_records(
        category=category_map[req.category],
        start_date=req.start_date,
        end_date=req.end_date,
        keyword=req.keyword,
        ship_type=req.ship_type,
        limit=req.limit,
    )
    return {"success": True, "count": len(records), "data": records}


@router.post("/summary")
async def get_market_summary(
    req: MarketAnalysisRequest,
    current_user: User = Depends(_require_admin),
) -> Dict[str, Any]:
    """查询市场汇总统计（按日期、按船型），含移动平均。"""
    category_map = {"newbuilding": "新造船订单", "sale": "买卖船", "charter": "租船"}
    if req.category not in category_map:
        raise HTTPException(status_code=400, detail=f"不支持的市场类别: {req.category}")

    records = fetch_market_records(
        category=category_map[req.category],
        start_date=req.start_date,
        end_date=req.end_date,
        keyword=req.keyword,
        ship_type=req.ship_type,
        limit=req.limit,
    )

    date_agg = aggregate_by_date(records)
    ship_type_agg = aggregate_by_ship_type(records)

    # 为日期聚合补充 MA 线
    avg_prices = [d.get("avg_price") for d in date_agg]
    ma5 = compute_ma(avg_prices, window=5)
    ma10 = compute_ma(avg_prices, window=10)
    for i, d in enumerate(date_agg):
        d["ma5"] = ma5[i] if i < len(ma5) else None
        d["ma10"] = ma10[i] if i < len(ma10) else None

    return {
        "success": True,
        "count": len(records),
        "category": req.category,
        "date_summary": date_agg,
        "ship_type_summary": ship_type_agg,
    }


@router.get("/last-sync")
async def last_sync(
    category: str = Query(..., description="newbuilding | sale | charter"),
    current_user: User = Depends(_require_admin),
) -> Dict[str, Any]:
    """返回指定市场类别在对应市场分析表中的最新更新时间。"""
    category_map = {"newbuilding": "新造船订单", "sale": "买卖船", "charter": "租船"}
    if category not in category_map:
        raise HTTPException(status_code=400, detail=f"不支持的市场类别: {category}")

    ts = get_last_sync_time(category_map[category])
    return {"success": True, "category": category, "last_sync_at": ts}


@router.put("/records/{record_id}")
async def update_record(
    record_id: int,
    req: UpdateMarketRecordRequest,
    current_user: User = Depends(_require_admin),
) -> Dict[str, Any]:
    """修改指定类别市场分析表中的单条交易记录。"""
    category_map = {"newbuilding": "新造船订单", "sale": "买卖船", "charter": "租船"}
    if req.category not in category_map:
        raise HTTPException(status_code=400, detail=f"不支持的市场类别: {req.category}")

    try:
        updates = {k: v for k, v in req.model_dump().items() if v is not None and k != "category"}
        updated = update_market_record(record_id, req.category, updates)
        return {"success": True, "data": [updated]}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新失败: {e}")


@router.post("/sync")
async def sync_market_analysis(
    req: MarketAnalysisSyncRequest,
    current_user: User = Depends(_require_admin),
) -> Dict[str, Any]:
    """启动市场分析交易数据同步后台任务，立即返回 task_id 供轮询。"""
    task_id = f"sync-{uuid.uuid4().hex}"
    asyncio.create_task(
        _run_sync_task(
            task_id,
            req.category,
            req.start_date,
            req.end_date,
            req.full_rebuild,
        )
    )
    return {
        "success": True,
        "task_id": task_id,
        "message": "同步任务已启动，请通过 /market-analysis/sync/{task_id} 查询进度",
    }


@router.get("/sync/{task_id}")
async def get_sync_task_status(
    task_id: str,
    current_user: User = Depends(_require_admin),
) -> Dict[str, Any]:
    """查询市场分析同步任务状态。"""
    async with _sync_tasks_lock:
        task = _sync_tasks.get(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")

    response = {
        "success": True,
        "task_id": task_id,
        "status": task["status"],
    }
    if task["status"] == "completed":
        response.update(task["result"] or {})
    elif task["status"] == "failed":
        response["error"] = task["error"]
    return response


@router.get("/ship-types")
async def list_ship_types(
    current_user: User = Depends(_require_admin),
) -> Dict[str, Any]:
    """返回船型词表中的细分船型列表，供市场分析趋势图选择使用。"""
    entries = _load_ship_type_taxonomy()
    ship_types: List[Dict[str, Any]] = [
        {
            "canonical_name": e.get("canonical_name"),
            "category": e.get("category") or e.get("level_1") or "其他",
            "level": e.get("level"),
        }
        for e in entries
        if e.get("level", 0) >= 2 and e.get("canonical_name")
    ]
    ship_types.sort(key=lambda x: (x["category"], x["canonical_name"]))
    return {
        "success": True,
        "count": len(ship_types),
        "ship_types": ship_types,
    }

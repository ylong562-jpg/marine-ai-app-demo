# -*- coding: utf-8 -*-
"""
船厂六维评分 API。

- GET  /shipyard-dimensions/{shipyard_id}：读取 shipyard_dimension_scores 表
- POST /shipyard-dimensions/recompute：管理员触发重算（后台子进程，
  复用 app/scripts/compute_shipyard_dimensions.py，鉴权参照 app/api/reprocess.py）
"""
import os
import subprocess
import sys
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.auth import _is_admin
from app.core.config import get_settings
from app.core.security import get_current_user
from app.db.models import User
from app.shipyard_dimensions import DIMENSIONS
from app.shipyard_dimensions import db as sdim_db

router = APIRouter(prefix="/shipyard-dimensions", tags=["Shipyard Dimensions"])
settings = get_settings()

# backend 目录（基于本文件位置推导：<repo>/backend）
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Require admin or core admin."""
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


class RecomputeRequest(BaseModel):
    shipyard_ids: List[int] = Field(default_factory=list, description="船厂 ID 列表（空=全部有订单映射的船厂）")
    fit_weights: bool = Field(default=False, description="是否重新 NNLS 拟合权重")
    news_limit: int = Field(default=200, description="每家船厂新闻条数上限")


@router.get("/{shipyard_id}")
async def get_shipyard_dimensions(
    shipyard_id: int,
    current_user: User = Depends(get_current_user),
):
    """返回船厂六维评分（数据来自 shipyard_dimension_scores 表）。"""
    conn = sdim_db.get_connection()
    try:
        shipyard = sdim_db.get_shipyard(conn, shipyard_id)
        if shipyard is None:
            raise HTTPException(status_code=404, detail=f"船厂不存在: shipyard_id={shipyard_id}")
        rows = sdim_db.load_dimension_scores(conn, shipyard_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {e}")
    finally:
        conn.close()

    dimensions = {}
    model_version = None
    computed_at = None
    for row in rows:
        name = row["dimension"]
        if name not in DIMENSIONS:
            continue
        dimensions[name] = {
            "score": row["score"],
            "explanation": row.get("explanation") or "",
            "evidence_count": row.get("evidence_count") or 0,
        }
        model_version = model_version or row.get("model_version")
        computed_at = computed_at or row.get("computed_at")

    note = None
    if not dimensions:
        note = "该船厂暂无评分数据，可联系管理员运行重算任务"
    return {
        "success": True,
        "shipyard_id": shipyard_id,
        "shipyard_name": shipyard.get("Local_name") or shipyard.get("English_name"),
        "model_version": model_version,
        "computed_at": computed_at,
        "dimensions": dimensions,
        "note": note,
    }


def _run_recompute_task(task_id: str, cmd: list):
    output_dir = os.path.join(_BACKEND_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, f"shipyard_dimensions_{task_id}.log")
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"[{datetime.now().isoformat()}] start task {task_id}\n")
        log_file.flush()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=86400)
            log_file.write(proc.stdout or "")
            if proc.stderr:
                log_file.write("\n[STDERR]\n")
                log_file.write(proc.stderr)
            log_file.write(f"\n[EXIT CODE] {proc.returncode}\n")
        except subprocess.TimeoutExpired:
            log_file.write("\n[TIMEOUT] 任务超时\n")
        except Exception as e:  # noqa: BLE001
            log_file.write(f"\n[ERROR] {e}\n")


@router.post("/recompute")
async def recompute_dimensions(
    req: RecomputeRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(_require_admin),
):
    """管理员触发船厂六维评分重算（后台运行，仅管理员）。"""
    script_path = os.path.join(_BACKEND_DIR, "app", "scripts", "compute_shipyard_dimensions.py")
    if not os.path.exists(script_path):
        raise HTTPException(status_code=500, detail="评分计算脚本未找到")
    if req.news_limit < 1:
        raise HTTPException(status_code=400, detail="news_limit 必须大于 0")

    cmd = [sys.executable, script_path, "--news-limit", str(req.news_limit)]
    if req.fit_weights:
        cmd.append("--fit-weights")
    if req.shipyard_ids:
        for sid in req.shipyard_ids:
            cmd.extend(["--shipyard-id", str(sid)])
    else:
        cmd.append("--all")

    task_id = str(uuid.uuid4())
    background_tasks.add_task(_run_recompute_task, task_id, cmd)
    return {
        "success": True,
        "task_id": task_id,
        "message": "六维评分重算任务已在后台启动，可查看 output/shipyard_dimensions_<task_id>.log",
    }

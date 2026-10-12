"""
航运相关指数 API endpoints.
连接 crawler_domestic 使用的 Time_Series.daily_data 表。
"""
import re
from datetime import datetime
from typing import Any, Optional

import mysql.connector
import os
import shutil
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.auth import _is_admin
from app.core.security import get_current_user
from app.db.models import User
from app.services.shipping_index_report import generate_latest_image, generate_weekly_report

router = APIRouter(prefix="/shipping-index", tags=["Shipping Index"])

# crawler_domestic 数据库配置（与爬虫项目 crawler_domestic/main.py 一致）
CRAWLER_DB_CONFIG = {
    "host": "your-db-host.example.com",
    "port": 3306,
    "user": "your-db-user",
    "password": "your-db-password",
    "database": "Time_Series",
}

DAILY_DATA_TABLE = "daily_data"


async def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


def _get_connection():
    try:
        return mysql.connector.connect(**CRAWLER_DB_CONFIG)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"数据库连接失败: {e}",
        )


def _get_all_columns(conn) -> list[str]:
    cursor = conn.cursor()
    try:
        cursor.execute(f"DESCRIBE {DAILY_DATA_TABLE}")
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()


def _get_numeric_columns(conn) -> list[str]:
    """返回除 date 外的所有列名（按 DESCRIBE 顺序）。"""
    cols = _get_all_columns(conn)
    return [c for c in cols if c.lower() != "date"]


def _detect_column_scales(rows: list[dict], columns: list[str]) -> dict[str, int]:
    """
    根据已有数据检测每列应保留的小数位数。
    取该列所有非空值中最大的小数位数。
    """
    scales: dict[str, int] = {col: 0 for col in columns}
    for col in columns:
        max_scale = 0
        for row in rows:
            raw = row.get(col)
            if raw is None or raw == "":
                continue
            s = str(raw).strip()
            if "." in s:
                # 去掉末尾的 0 后再计算真实精度？
                # 这里遵循“数据库中数字的保留位数格式”，因此按原样长度计算。
                decimal_part = s.split(".")[1]
                max_scale = max(max_scale, len(decimal_part))
        scales[col] = max_scale
    return scales


def _format_value(value: Any, scale: int) -> Any:
    """按检测到的精度格式化数值。"""
    if value is None or value == "":
        return None
    try:
        f = float(value)
        if scale <= 0:
            return str(int(round(f)))
        return f"{f:.{scale}f}"
    except (ValueError, TypeError):
        return str(value)


class ListColumnsResponse(BaseModel):
    success: bool
    columns: list[str]


class QueryDataRequest(BaseModel):
    start_date: Optional[str] = Field(default=None, description="开始日期 YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="结束日期 YYYY-MM-DD")
    columns: Optional[list[str]] = Field(default=None, description="要查询的列，默认全部")
    limit: Optional[int] = Field(default=None, description="最大返回条数，默认不限制")


class QueryDataResponse(BaseModel):
    success: bool
    columns: list[str]
    rows: list[dict[str, Any]]
    scales: dict[str, int]
    message: str


class UpdateDataRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list, description="要更新的行，每行必须包含 date")


class UpdateDataResponse(BaseModel):
    success: bool
    updated: int
    unchanged: int
    message: str


class GenerateLatestImageResponse(BaseModel):
    success: bool
    message: str


class GenerateWeeklyReportRequest(BaseModel):
    use_ai: bool = Field(default=True, description="是否使用 AI 生成分析文本，失败时自动回退规则文本")


@router.get("/columns", response_model=ListColumnsResponse)
async def list_columns(current_user: User = Depends(_require_admin)):
    """返回 daily_data 表除 date 外的所有列名。"""
    conn = _get_connection()
    try:
        columns = _get_numeric_columns(conn)
        return {"success": True, "columns": columns}
    finally:
        conn.close()


@router.post("/data", response_model=QueryDataResponse)
async def query_data(req: QueryDataRequest, current_user: User = Depends(_require_admin)):
    """按日期范围、列名查询 daily_data 数据，日期倒序。"""
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        all_numeric_cols = _get_numeric_columns(conn)
        selected_cols = [c for c in (req.columns or all_numeric_cols) if c in all_numeric_cols]
        if not selected_cols:
            raise HTTPException(status_code=400, detail="未选择有效列")

        conditions = []
        params = []
        if req.start_date:
            conditions.append("date >= %s")
            params.append(req.start_date)
        if req.end_date:
            conditions.append("date <= %s")
            params.append(req.end_date)

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        # 可选限制条数
        limit_clause = ""
        if req.limit is not None and req.limit > 0:
            limit_clause = "LIMIT %s"
            params.append(req.limit)

        query = f"""
            SELECT date, {', '.join(selected_cols)}
            FROM {DAILY_DATA_TABLE}
            {where_clause}
            ORDER BY date DESC
            {limit_clause}
        """
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()

        # 格式化日期为字符串，None 值统一为 ""
        formatted_rows = []
        for row in rows:
            formatted = {"date": row.get("date")}
            if isinstance(formatted["date"], datetime):
                formatted["date"] = formatted["date"].strftime("%Y-%m-%d")
            for col in selected_cols:
                val = row.get(col)
                if val is None:
                    formatted[col] = ""
                elif isinstance(val, float):
                    # 去掉 float 可能引入的科学计数/末尾浮点误差，按原字符串思路保留
                    formatted[col] = str(val)
                else:
                    formatted[col] = str(val)
            formatted_rows.append(formatted)

        scales = _detect_column_scales(formatted_rows, selected_cols)

        return {
            "success": True,
            "columns": ["date"] + selected_cols,
            "rows": formatted_rows,
            "scales": scales,
            "message": f"查询成功，共 {len(formatted_rows)} 条",
        }
    finally:
        cursor.close()
        conn.close()


@router.post("/generate-latest-image")
async def generate_latest_image_endpoint(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(_require_admin),
):
    """生成最新数据 PNG 图片并下载。"""
    try:
        png_bytes, filename = await generate_latest_image()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成最新数据图片失败: {e}")

    tmpdir = tempfile.mkdtemp(prefix="shipping_index_latest_")
    output_path = os.path.join(tmpdir, filename)
    with open(output_path, "wb") as f:
        f.write(png_bytes)
    background_tasks.add_task(shutil.rmtree, tmpdir, ignore_errors=True)

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return FileResponse(output_path, headers=headers, media_type="image/png", filename=filename)


@router.post("/generate-weekly-report")
async def generate_weekly_report_endpoint(
    req: GenerateWeeklyReportRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(_require_admin),
):
    """生成上周数据 HTML 报告并下载。"""
    try:
        html_bytes, filename = await generate_weekly_report(use_ai=req.use_ai)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成周报失败: {e}")

    tmpdir = tempfile.mkdtemp(prefix="shipping_index_weekly_")
    output_path = os.path.join(tmpdir, filename)
    with open(output_path, "wb") as f:
        f.write(html_bytes)
    background_tasks.add_task(shutil.rmtree, tmpdir, ignore_errors=True)

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return FileResponse(output_path, headers=headers, media_type="text/html", filename=filename)


@router.post("/update", response_model=UpdateDataResponse)
async def update_data(req: UpdateDataRequest, current_user: User = Depends(_require_admin)):
    """将用户修改的数据更新回 daily_data 表，仅更新有变化的单元格。"""
    if not req.rows:
        return {"success": True, "updated": 0, "unchanged": 0, "message": "没有要更新的数据"}

    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        numeric_cols = _get_numeric_columns(conn)

        # 获取当前列精度
        # 先读取所有相关日期的原始数据以检测精度和比对变化
        dates = [r.get("date") for r in req.rows if r.get("date")]
        if not dates:
            raise HTTPException(status_code=400, detail="缺少日期字段")

        placeholders = ", ".join(["%s"] * len(dates))
        cursor.execute(
            f"SELECT * FROM {DAILY_DATA_TABLE} WHERE date IN ({placeholders})",
            tuple(dates),
        )
        existing_rows = {str(r["date"]): r for r in cursor.fetchall()}

        # 检测精度
        scales = _detect_column_scales(existing_rows.values(), numeric_cols)

        updated_count = 0
        unchanged_count = 0
        for row in req.rows:
            date = row.get("date")
            if not date:
                unchanged_count += 1
                continue

            date_str = str(date)
            existing = existing_rows.get(date_str)
            if not existing:
                unchanged_count += 1
                continue

            changed_cols = []
            changed_values = []
            for col in numeric_cols:
                if col not in row:
                    continue
                new_val_raw = row[col]
                old_val_raw = existing.get(col)

                # 统一字符串比较
                old_str = "" if old_val_raw is None else str(old_val_raw)
                new_str = "" if new_val_raw is None or new_val_raw == "" else str(new_val_raw)

                if old_str == new_str:
                    continue

                # 按列精度格式化新值
                formatted = _format_value(new_val_raw, scales.get(col, 0))
                changed_cols.append(col)
                changed_values.append(formatted)

            if not changed_cols:
                unchanged_count += 1
                continue

            # 逐列更新
            for col, value in zip(changed_cols, changed_values):
                cursor.execute(
                    f"UPDATE {DAILY_DATA_TABLE} SET {col} = %s WHERE date = %s",
                    (value, date_str),
                )
                updated_count += 1

        conn.commit()
        if updated_count == 0:
            return {
                "success": True,
                "updated": 0,
                "unchanged": unchanged_count,
                "message": "没有数据发生变更，未执行更新",
            }
        return {
            "success": True,
            "updated": updated_count,
            "unchanged": unchanged_count,
            "message": f"成功更新 {updated_count} 个单元格",
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"更新失败: {e}")
    finally:
        cursor.close()
        conn.close()

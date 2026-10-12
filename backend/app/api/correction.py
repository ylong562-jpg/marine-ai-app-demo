# -*- coding: utf-8 -*-
"""
人工矫正模块 API

提供待矫正新闻的查询、确认入库、跳过等功能。
确认无误的报道会写入 Time_Series.verified_news 表，与 ship_news 通过 id 配对。
"""
from datetime import datetime
from typing import Optional

import mysql.connector
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.auth import _is_admin
from app.core.config import get_settings
from app.core.security import get_current_user
from app.db.models import User

settings = get_settings()
router = APIRouter(prefix="/correction", tags=["Correction"])


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


def _get_connection():
    try:
        return mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"数据库连接失败: {e}",
        )


def _ensure_verified_table():
    """Ensure the verified_news table exists. Called once at module load."""
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {settings.VERIFIED_NEWS_TABLE} (
                ship_news_id INT PRIMARY KEY,
                link TEXT,
                news_report TEXT,
                verified_by VARCHAR(64),
                verified_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_verified_at (verified_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


# Create table on module load (project has no formal migration tool)
_ensure_verified_table()


class CorrectionPendingFilters(BaseModel):
    start_date: Optional[str] = Field(default=None, description="开始日期 YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="结束日期 YYYY-MM-DD")
    limit: Optional[int] = Field(default=1000, description="最大返回条数")


class VerifyNewsRequest(BaseModel):
    ship_news_id: int = Field(..., description="ship_news 表中的新闻 ID")
    link: Optional[str] = Field(default=None, description="新闻链接")
    news_report: Optional[str] = Field(default=None, description="确认后的报道内容")


class SkipNewsRequest(BaseModel):
    ship_news_id: int = Field(..., description="ship_news 表中的新闻 ID")


@router.get("/pending")
async def get_pending_correction_news(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: Optional[int] = Query(1000, description="最大返回条数"),
    current_user: User = Depends(_require_admin),
):
    """获取指定日期范围内待人工矫正的新闻，排除已校验记录。"""
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        conditions = []
        params = []

        if start_date:
            conditions.append("n.date >= %s")
            params.append(start_date)
        if end_date:
            conditions.append("n.date <= %s")
            params.append(end_date)

        # Exclude news already verified
        exclude_sql = f"""
            NOT EXISTS (
                SELECT 1 FROM {settings.VERIFIED_NEWS_TABLE} v
                WHERE v.ship_news_id = n.id
            )
        """
        conditions.append(exclude_sql)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"""
            SELECT n.* FROM {settings.NEWS_TABLE} n
            {where_clause}
            ORDER BY n.date DESC, n.id DESC
        """
        if limit:
            query += " LIMIT %s"
            params.append(int(limit))

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # Enrich display fields to match news management
        results = []
        for row in rows:
            row = _enrich_row(row)
            results.append(row)

        return {
            "success": True,
            "count": len(results),
            "data": results,
        }
    finally:
        cursor.close()
        conn.close()


@router.post("/verify")
async def verify_news(
    req: VerifyNewsRequest,
    current_user: User = Depends(_require_admin),
):
    """将确认无误的新闻报道写入 verified_news 表。"""
    if not req.news_report or not req.news_report.strip():
        raise HTTPException(status_code=400, detail="报道内容不能为空")

    conn = _get_connection()
    cursor = conn.cursor()
    try:
        # Upsert: if already verified, overwrite with latest correction
        query = f"""
            INSERT INTO {settings.VERIFIED_NEWS_TABLE}
                (ship_news_id, link, news_report, verified_by, verified_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                link = VALUES(link),
                news_report = VALUES(news_report),
                verified_by = VALUES(verified_by),
                verified_at = VALUES(verified_at)
        """
        cursor.execute(
            query,
            (
                req.ship_news_id,
                req.link or "",
                req.news_report,
                current_user.username,
            ),
        )
        conn.commit()
        return {
            "success": True,
            "message": "新闻已确认并入库",
            "ship_news_id": req.ship_news_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"确认入库失败: {e}")
    finally:
        cursor.close()
        conn.close()


@router.post("/skip")
async def skip_news(
    req: SkipNewsRequest,
    current_user: User = Depends(_require_admin),
):
    """跳过当前新闻（前端 workflow 步骤，不写入 verified_news）。"""
    return {
        "success": True,
        "message": "已跳过",
        "ship_news_id": req.ship_news_id,
    }


def _parse_json_safe(value):
    import json

    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _enrich_row(row: dict) -> dict:
    """Add derived display fields consistent with news management."""
    import json

    tags = _parse_json_safe(row.get("tags"))
    main_tags = []
    if isinstance(tags, dict):
        main_tags = tags.get("main_tags", [])
    row["main_tags"] = ", ".join(main_tags) if main_tags else row.get("category", "")
    return row

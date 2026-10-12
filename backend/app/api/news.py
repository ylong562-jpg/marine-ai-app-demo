"""
News intelligence API endpoints.
Connects to the Time_Series MySQL database for ship_news table.
"""
import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

import mysql.connector
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import _is_admin
from app.core.config import get_settings
from app.core.security import get_current_user
from app.db.models import User

# 将 app/news_engine 加入 Python 路径，以便导入 core.reprocess_database_news
_NEWS_ENGINE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine"
)
if _NEWS_ENGINE_DIR not in sys.path:
    sys.path.insert(0, _NEWS_ENGINE_DIR)

# backend 目录（基于本文件位置推导：<repo>/backend）
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

router = APIRouter(prefix="/news", tags=["News"])
settings = get_settings()


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

VPN_NEEDED_PLATFORMS = {"Seatrade", "Splash247", "TradeWinds"}

SEARCH_SCOPE_OPTIONS = [
    "title_summary_report",
    "entity",
    "data",
    "tags",
    "link",
    "all",
]


class NewsUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None)
    summary: Optional[str] = Field(default=None)
    news_report: Optional[str] = Field(default=None)
    category: Optional[str] = Field(default=None)
    tags: Optional[str] = Field(default=None)
    entity: Optional[str] = Field(default=None)
    data: Optional[str] = Field(default=None)
    link: Optional[str] = Field(default=None)
    date: Optional[str] = Field(default=None)
    source_platform: Optional[str] = Field(default=None)
    confidence_score: Optional[float] = Field(default=None)


class GenerateWebpageRequest(BaseModel):
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")
    vpn_filter: Optional[str] = Field(default=None, description="all/no_vpn/vpn")


class RefreshBodyRequest(BaseModel):
    days: int = Field(default=7, description="最近 N 天")
    limit: Optional[int] = Field(default=None, description="限制条数")
    start_date: Optional[str] = Field(default=None, description="开始日期 YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="结束日期 YYYY-MM-DD")
    keyword: Optional[str] = Field(default=None, description="关键字")
    search_scope: Optional[str] = Field(default="title_summary_report", description="检索范围")
    reprocess_steps: Optional[list[str]] = Field(default=None, description="处理步骤")
    force: bool = Field(default=False, description="强制重新生成，不跳过已处理的新闻")


class RefreshBodySingleRequest(BaseModel):
    news_id: int = Field(..., description="新闻 ID")
    manual_summary: Optional[str] = Field(default=None, description="用户手动输入的新闻正文")


class ReprocessPreviewRequest(BaseModel):
    news_ids: list[int] = Field(default_factory=list, description="要重新处理的新闻 ID 列表")


class RefreshReportPreviewRequest(BaseModel):
    news_ids: list[int] = Field(default_factory=list, description="要刷新正文的新闻 ID 列表")


class BatchUpdateItem(NewsUpdateRequest):
    id: int = Field(..., description="新闻 ID")


class BatchUpdateRequest(BaseModel):
    items: list[BatchUpdateItem] = Field(default_factory=list, description="要批量更新的新闻列表")


class BatchDeleteRequest(BaseModel):
    ids: list[int] = Field(default_factory=list, description="要删除的新闻 ID 列表")


class CrawlPreviewRequest(BaseModel):
    vpn_mode: str = Field(default="all", description="all/no_vpn/vpn/wechat_only")
    start_date: Optional[str] = Field(default=None, description="开始日期 YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="结束日期 YYYY-MM-DD")
    limit: Optional[int] = Field(default=50, description="限制条数")


class ImportSelectedRequest(BaseModel):
    items: list[dict] = Field(default_factory=list, description="要导入的新闻列表")


class AnalyzeNewsRequest(BaseModel):
    source: str = Field(..., description="数据来源: database/url/text")
    start_date: Optional[str] = Field(default=None, description="开始日期 YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="结束日期 YYYY-MM-DD")
    date: Optional[str] = Field(default=None, description="新闻日期 YYYY-MM-DD")
    url: Optional[str] = Field(default=None, description="要分析的 URL")
    text: Optional[str] = Field(default=None, description="用户输入的文本")
    title: Optional[str] = Field(default=None, description="新闻标题")
    source_platform: Optional[str] = Field(default=None, description="来源平台")
    confidence_score: Optional[int] = Field(default=None, description="置信度")
    limit: Optional[int] = Field(default=None, description="限制条数")


def _get_connection():
    try:
        return mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"数据库连接失败: {e}",
        )


def _is_vpn_needed(platform: Optional[str]) -> bool:
    return platform in VPN_NEEDED_PLATFORMS


def _parse_json_safe(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _collect_text(obj, texts=None):
    if texts is None:
        texts = []
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_text(v, texts)
    elif isinstance(obj, list):
        for item in obj:
            _collect_text(item, texts)
    elif isinstance(obj, str):
        texts.append(obj)
    return texts


def _row_matches_search_scope(row: dict, keyword: str, scope: str) -> bool:
    if not keyword:
        return True
    kw = keyword.lower()

    def contains(value) -> bool:
        if value is None:
            return False
        return kw in str(value).lower()

    if scope == "title_summary_report":
        return any(contains(row.get(f)) for f in ("title", "summary", "news_report"))
    if scope == "link":
        return contains(row.get("link"))

    texts = []
    # entity 信息已并入 data，检索 entity 时同时搜索 data 和旧 entity 字段以保持兼容
    if scope in ("entity", "all"):
        entity = _parse_json_safe(row.get("entity"))
        if entity is not None:
            texts.extend(_collect_text(entity))
    if scope in ("entity", "data", "all"):
        data = _parse_json_safe(row.get("data"))
        if data is not None:
            texts.extend(_collect_text(data))
    if scope in ("tags", "all"):
        tags = _parse_json_safe(row.get("tags"))
        if tags is not None:
            texts.extend(_collect_text(tags))

    joined = "\n".join(texts).lower()
    if kw in joined:
        return True

    if scope == "all":
        return any(contains(row.get(f)) for f in ("title", "summary", "news_report", "link"))
    return False


def _row_has_category(row: dict, category: str) -> bool:
    if row.get("category") == category:
        return True
    tags = _parse_json_safe(row.get("tags"))
    if isinstance(tags, dict):
        main_tags = tags.get("main_tags", [])
        if isinstance(main_tags, list) and category in main_tags:
            return True
    return False


def _enrich_row(row: dict) -> dict:
    """Add derived display fields."""
    tags = _parse_json_safe(row.get("tags"))
    main_tags = []
    if isinstance(tags, dict):
        main_tags = tags.get("main_tags", [])
    row["main_tags"] = ", ".join(main_tags) if main_tags else row.get("category", "")
    row["vpn_needed"] = _is_vpn_needed(row.get("source_platform"))
    return row


@router.get("")
async def get_news(
    category: Optional[str] = Query(None, description="主维度分类"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    days: Optional[int] = Query(None, description="最近 N 天"),
    keyword: Optional[str] = Query(None, description="关键字"),
    search_scope: str = Query("title_summary_report", description="检索范围"),
    vpn_need: str = Query("all", description="VPN 需求: all/no_vpn/vpn"),
    platform: Optional[str] = Query(None, description="来源平台"),
    limit: Optional[int] = Query(1000, description="限制条数"),
    current_user: User = Depends(get_current_user),
):
    """Fetch news from ship_news table with filters."""
    if search_scope not in SEARCH_SCOPE_OPTIONS:
        raise HTTPException(status_code=400, detail="无效的检索范围")
    if vpn_need not in ("all", "no_vpn", "vpn"):
        raise HTTPException(status_code=400, detail="无效的 VPN 需求参数")

    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Build SQL filters
        conditions = []
        params = []

        if category:
            # 支持单个或多个主维度（逗号分隔），基于 tags.main_tags JSON 数组匹配
            cats = [c.strip() for c in category.split(",") if c.strip()]
            if cats:
                cat_conditions = []
                for cat in cats:
                    cat_conditions.append("JSON_CONTAINS(NULLIF(tags, ''), %s, '$.main_tags')")
                    params.append(json.dumps(cat))
                conditions.append(f"({' OR '.join(cat_conditions)})")

        if days is not None:
            start = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
            conditions.append("date >= %s")
            params.append(start)
        else:
            if start_date:
                conditions.append("date >= %s")
                params.append(start_date)
            if end_date:
                conditions.append("date <= %s")
                params.append(end_date)

        if platform:
            conditions.append("source_platform = %s")
            params.append(platform)

        # VPN need can be efficiently filtered in SQL
        if vpn_need == "vpn":
            placeholders = ", ".join(["%s"] * len(VPN_NEEDED_PLATFORMS))
            conditions.append(f"source_platform IN ({placeholders})")
            params.extend(list(VPN_NEEDED_PLATFORMS))
        elif vpn_need == "no_vpn":
            placeholders = ", ".join(["%s"] * len(VPN_NEEDED_PLATFORMS))
            conditions.append(f"source_platform NOT IN ({placeholders})")
            params.extend(list(VPN_NEEDED_PLATFORMS))

        # Keyword SQL filtering for title/summary/report/link
        if keyword:
            if search_scope == "title_summary_report":
                like = f"%{keyword}%"
                conditions.append("(title LIKE %s OR summary LIKE %s OR news_report LIKE %s)")
                params.extend([like, like, like])
            elif search_scope == "link":
                like = f"%{keyword}%"
                conditions.append("link LIKE %s")
                params.append(like)

        # entity/data/tags/all 范围需要在 Python 中对 JSON 字段做关键字匹配，
        # 若在 SQL 中先 LIMIT 再过滤，会导致“最近 N 条中无命中”时返回空结果。
        requires_python_filter = bool(keyword) and search_scope in ("entity", "data", "tags", "all")

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"SELECT * FROM {settings.NEWS_TABLE}{where} ORDER BY date DESC, id DESC"
        if limit and not requires_python_filter:
            query += " LIMIT %s"
            params.append(int(limit))

        cursor.execute(query, params)
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    # Client-side filtering for JSON scopes
    filtered = []
    for row in rows:
        if requires_python_filter:
            if not _row_matches_search_scope(row, keyword, search_scope):
                continue
        filtered.append(_enrich_row(row))
        if limit and len(filtered) >= int(limit):
            break

    return {
        "success": True,
        "count": len(filtered),
        "data": filtered,
    }


@router.put("/{news_id}")
async def update_news(
    news_id: int,
    req: NewsUpdateRequest,
    current_user: User = Depends(get_current_user),
):
    """Update a news record in the database."""
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=400, detail="没有要更新的字段")

        allowed_columns = set()
        cursor.execute(f"DESCRIBE {settings.NEWS_TABLE}")
        for row in cursor.fetchall():
            allowed_columns.add(row[0])

        # Ensure only existing columns are updated
        updates = {k: v for k, v in updates.items() if k in allowed_columns and k != "id"}
        if not updates:
            raise HTTPException(status_code=400, detail="没有有效的更新字段")

        fields = [f"{k} = %s" for k in updates.keys()]
        params = list(updates.values())
        params.append(news_id)

        query = f"UPDATE {settings.NEWS_TABLE} SET {', '.join(fields)} WHERE id = %s"
        cursor.execute(query, params)
        conn.commit()

        return {
            "success": True,
            "updated": cursor.rowcount,
            "message": "新闻更新成功",
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"更新失败: {e}")
    finally:
        cursor.close()
        conn.close()


@router.get("/platforms")
async def get_platforms(current_user: User = Depends(get_current_user)):
    """Return distinct source platforms."""
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT DISTINCT source_platform FROM {settings.NEWS_TABLE} "
            f"WHERE source_platform IS NOT NULL AND source_platform != '' "
            f"ORDER BY source_platform"
        )
        platforms = [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()
    return {"success": True, "platforms": platforms}


@router.get("/columns")
async def get_columns(current_user: User = Depends(get_current_user)):
    """Return table column names."""
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"DESCRIBE {settings.NEWS_TABLE}")
        columns = [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()
    return {"success": True, "columns": columns}


@router.post("/generate-webpage")
async def generate_webpage(
    req: GenerateWebpageRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Generate a daily news HTML webpage and return it as a downloadable file."""
    script_path = os.path.join(_BACKEND_DIR, "app", "scripts", "generate_news_webpage.py")
    if not os.path.exists(script_path):
        raise HTTPException(status_code=500, detail="新闻生成脚本未找到")

    python_bin = sys.executable

    tmpdir = tempfile.mkdtemp()

    env = os.environ.copy()
    env["PYTHONPATH"] = _NEWS_ENGINE_DIR
    env["OUTPUT_DIR"] = tmpdir

    cmd = [
        python_bin,
        script_path,
        req.start_date,
        req.end_date,
    ]
    if req.vpn_filter and req.vpn_filter != "all":
        cmd.append(req.vpn_filter)

    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"生成失败: {proc.stderr or proc.stdout}",
            )

        news_dir = os.path.join(tmpdir, "每日新闻")
        html_files = glob.glob(os.path.join(news_dir, "*.html"))
        if not html_files:
            raise HTTPException(status_code=500, detail="未找到生成的 HTML 文件")

        output_path = html_files[0]
        filename = os.path.basename(output_path)

        background_tasks.add_task(shutil.rmtree, tmpdir, True)

        headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
        return FileResponse(
            output_path,
            headers=headers,
            media_type="text/html",
            filename=filename,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, True)
        raise HTTPException(status_code=504, detail="生成超时")
    except Exception:
        shutil.rmtree(tmpdir, True)
        raise


def _run_script_in_background(script_name: str, cmd: list, log_prefix: str):
    """Run a Python script in background and append output to a log file."""
    log_dir = os.path.join(_BACKEND_DIR, "output")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{script_name}.log")
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n{'='*60}\n[{datetime.now().isoformat()}] {log_prefix}\n")
        log_file.flush()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            log_file.write(proc.stdout or "")
            if proc.stderr:
                log_file.write("\n[STDERR]\n")
                log_file.write(proc.stderr)
            log_file.write(f"\n[EXIT CODE] {proc.returncode}\n")
        except subprocess.TimeoutExpired:
            log_file.write("\n[TIMEOUT] 处理超过 30 分钟\n")
        except Exception as e:
            log_file.write(f"\n[ERROR] {e}\n")


@router.post("/refresh-body")
async def refresh_body(
    req: RefreshBodyRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Reprocess existing news in the database in background."""
    script_path = os.path.join(_BACKEND_DIR, "app", "scripts", "run_refresh_body.py")
    python_bin = sys.executable
    if not os.path.exists(script_path):
        raise HTTPException(status_code=500, detail="刷新正文脚本未找到")

    cmd = [python_bin, script_path]
    if req.start_date:
        cmd.extend(["--start-date", req.start_date])
    elif req.days is not None:
        cmd.extend(["--days", str(req.days)])
    else:
        cmd.extend(["--days", "7"])
    if req.end_date:
        cmd.extend(["--end-date", req.end_date])
    if req.limit:
        cmd.extend(["--limit", str(req.limit)])
    if req.keyword:
        cmd.extend(["--keyword", req.keyword])
    if req.search_scope:
        cmd.extend(["--search-scope", req.search_scope])
    if req.reprocess_steps:
        cmd.extend(["--steps", ",".join(req.reprocess_steps)])
    if req.force:
        cmd.append("--force")

    desc = f"刷新正文 days={req.days} start={req.start_date} end={req.end_date} limit={req.limit} keyword={req.keyword} scope={req.search_scope} steps={req.reprocess_steps} force={req.force}"
    background_tasks.add_task(
        _run_script_in_background,
        "refresh_body",
        cmd,
        desc,
    )
    return {
        "success": True,
        "message": "刷新正文任务已在后台启动，可查看 output/refresh_body.log",
    }


@router.post("/refresh-body-single")
async def refresh_body_single(
    req: RefreshBodySingleRequest,
    current_user: User = Depends(get_current_user),
):
    """Reprocess a single news item synchronously."""
    import asyncio
    from core.reprocess_database_news import reprocess_single_news_by_id

    try:
        result = await asyncio.wait_for(
            reprocess_single_news_by_id(req.news_id, req.manual_summary),
            timeout=120,
        )
        return result
    except asyncio.TimeoutError:
        # 获取新闻链接用于提示
        conn = _get_connection()
        cursor = conn.cursor(dictionary=True)
        link = ""
        title = ""
        try:
            cursor.execute(
                f"SELECT title, link FROM {settings.NEWS_TABLE} WHERE id = %s",
                (req.news_id,),
            )
            row = cursor.fetchone()
            if row:
                link = row.get("link") or ""
                title = row.get("title") or ""
        finally:
            cursor.close()
            conn.close()
        raise HTTPException(
            status_code=504,
            detail=json.dumps(
                {
                    "error": "刷新正文超时",
                    "message": "当前 IP 抓取/分析受限，处理超时。请手动输入新闻正文后重试。",
                    "news_id": req.news_id,
                    "title": title,
                    "link": link,
                    "needs_manual_input": True,
                },
                ensure_ascii=False,
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        # 获取新闻链接用于提示
        conn = _get_connection()
        cursor = conn.cursor(dictionary=True)
        link = ""
        title = ""
        try:
            cursor.execute(
                f"SELECT title, link FROM {settings.NEWS_TABLE} WHERE id = %s",
                (req.news_id,),
            )
            row = cursor.fetchone()
            if row:
                link = row.get("link") or ""
                title = row.get("title") or ""
        finally:
            cursor.close()
            conn.close()
        raise HTTPException(
            status_code=500,
            detail=json.dumps(
                {
                    "error": "刷新正文失败",
                    "message": f"处理失败：{str(e)}。当前 IP 可能已被限制，请尝试手动输入新闻正文后重试。",
                    "news_id": req.news_id,
                    "title": title,
                    "link": link,
                    "needs_manual_input": True,
                },
                ensure_ascii=False,
            ),
        )


def _calculate_subprocess_timeout(news_count: int) -> int:
    """根据新闻数量动态计算子进程超时时间（秒）。"""
    base = 1800  # 30 分钟基础值
    if news_count <= 50:
        return base
    # 超过 50 条后，每多一条增加 12 秒，上限 1 小时
    extra = (news_count - 50) * 12
    return min(base + extra, 3600)


def _run_reprocess_preview_task(task_id: str, news_ids: list[int]):
    """在后台子进程中运行新闻重新生成预览任务。"""
    output_dir = os.path.join(_BACKEND_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    input_path = os.path.join(output_dir, f"reprocess_preview_{task_id}_input.json")
    result_path = os.path.join(output_dir, f"reprocess_preview_{task_id}_result.json")
    log_path = os.path.join(output_dir, f"reprocess_preview_{task_id}.log")

    # 写入输入文件，包含并发配置
    input_payload = {
        "news_ids": news_ids,
        "max_concurrent": 8,
    }
    with open(input_path, "w", encoding="utf-8") as f:
        json.dump(input_payload, f, ensure_ascii=False, default=str)

    script_path = os.path.join(_BACKEND_DIR, "app", "scripts", "run_reprocess_preview.py")
    python_bin = sys.executable
    if not os.path.exists(script_path):
        _write_result(result_path, {"status": "error", "count": 0, "total": 0, "data": [], "error": "重新生成脚本未找到"})
        return

    timeout = _calculate_subprocess_timeout(len(news_ids))
    cmd = [python_bin, script_path, input_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(proc.stdout or "")
            if proc.stderr:
                f.write("\n[STDERR]\n")
                f.write(proc.stderr)
            f.write(f"\n[EXIT CODE] {proc.returncode}\n")
    except subprocess.TimeoutExpired:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[TIMEOUT] 重新生成任务超过 {timeout} 秒\n")
        _write_result(result_path, {"status": "error", "count": 0, "total": len(news_ids), "data": [], "error": "任务超时"})
    except Exception as e:
        err = traceback.format_exc()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[ERROR] {e}\n{err}\n")
        _write_result(result_path, {"status": "error", "count": 0, "total": len(news_ids), "data": [], "error": str(e)})


def _run_refresh_report_preview_task(task_id: str, news_ids: list[int]):
    """在后台子进程中运行刷新正文预览任务。"""
    output_dir = os.path.join(_BACKEND_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    input_path = os.path.join(output_dir, f"refresh_report_preview_{task_id}_input.json")
    result_path = os.path.join(output_dir, f"refresh_report_preview_{task_id}_result.json")
    log_path = os.path.join(output_dir, f"refresh_report_preview_{task_id}.log")

    input_payload = {
        "news_ids": news_ids,
        "max_concurrent": 8,
    }
    with open(input_path, "w", encoding="utf-8") as f:
        json.dump(input_payload, f, ensure_ascii=False, default=str)

    script_path = os.path.join(_BACKEND_DIR, "app", "scripts", "run_refresh_report_preview.py")
    python_bin = sys.executable
    if not os.path.exists(script_path):
        _write_result(result_path, {"status": "error", "count": 0, "total": 0, "data": [], "error": "刷新正文脚本未找到"})
        return

    timeout = _calculate_subprocess_timeout(len(news_ids))
    cmd = [python_bin, script_path, input_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(proc.stdout or "")
            if proc.stderr:
                f.write("\n[STDERR]\n")
                f.write(proc.stderr)
            f.write(f"\n[EXIT CODE] {proc.returncode}\n")
    except subprocess.TimeoutExpired:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[TIMEOUT] 刷新正文任务超过 {timeout} 秒\n")
        _write_result(result_path, {"status": "error", "count": 0, "total": len(news_ids), "data": [], "error": "任务超时"})
    except Exception as e:
        err = traceback.format_exc()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[ERROR] {e}\n{err}\n")
        _write_result(result_path, {"status": "error", "count": 0, "total": len(news_ids), "data": [], "error": str(e)})


def _write_result(path: str, payload: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)


@router.post("/reprocess-preview")
async def reprocess_preview(
    req: ReprocessPreviewRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """根据 ID 列表启动新闻重新生成预览任务（后台运行）。"""
    if not req.news_ids:
        raise HTTPException(status_code=400, detail="没有要重新处理的新闻")

    task_id = str(uuid.uuid4())
    output_dir = os.path.join(_BACKEND_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, f"reprocess_preview_{task_id}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] start task {task_id}\n")

    background_tasks.add_task(_run_reprocess_preview_task, task_id, req.news_ids)
    return {
        "success": True,
        "task_id": task_id,
        "message": "重新生成任务已启动",
    }


@router.get("/reprocess-preview/{task_id}")
async def get_reprocess_preview(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """查询新闻重新生成预览任务状态（支持返回运行中的部分结果）。"""
    output_dir = os.path.join(_BACKEND_DIR, "output")
    result_path = os.path.join(output_dir, f"reprocess_preview_{task_id}_result.json")
    log_path = os.path.join(output_dir, f"reprocess_preview_{task_id}.log")

    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            task_status = payload.get("status", "running")
            if task_status == "error":
                raise HTTPException(status_code=500, detail=payload.get("error", "重新生成失败"))
            data = payload.get("data", [])
            enriched = [_enrich_row(row) for row in data]
            return {
                "success": True,
                "status": task_status,
                "count": len(enriched),
                "total": payload.get("total", len(enriched)),
                "data": enriched,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析重新生成结果失败: {e}")

    if os.path.exists(log_path):
        return {
            "success": True,
            "status": "running",
            "count": 0,
            "total": 0,
            "data": [],
        }

    raise HTTPException(status_code=404, detail="任务不存在")


@router.post("/refresh-report-preview")
async def refresh_report_preview(
    req: RefreshReportPreviewRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """根据 ID 列表启动刷新正文预览任务（后台运行）。"""
    if not req.news_ids:
        raise HTTPException(status_code=400, detail="没有要刷新正文的新闻")

    task_id = str(uuid.uuid4())
    output_dir = os.path.join(_BACKEND_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, f"refresh_report_preview_{task_id}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] start task {task_id}\n")

    background_tasks.add_task(_run_refresh_report_preview_task, task_id, req.news_ids)
    return {
        "success": True,
        "task_id": task_id,
        "message": "刷新正文任务已启动",
    }


@router.get("/refresh-report-preview/{task_id}")
async def get_refresh_report_preview(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """查询刷新正文预览任务状态（支持返回运行中的部分结果）。"""
    output_dir = os.path.join(_BACKEND_DIR, "output")
    result_path = os.path.join(output_dir, f"refresh_report_preview_{task_id}_result.json")
    log_path = os.path.join(output_dir, f"refresh_report_preview_{task_id}.log")

    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            task_status = payload.get("status", "running")
            if task_status == "error":
                raise HTTPException(status_code=500, detail=payload.get("error", "刷新正文失败"))
            data = payload.get("data", [])
            enriched = [_enrich_row(row) for row in data]
            return {
                "success": True,
                "status": task_status,
                "count": len(enriched),
                "total": payload.get("total", len(enriched)),
                "data": enriched,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析刷新正文结果失败: {e}")

    if os.path.exists(log_path):
        return {
            "success": True,
            "status": "running",
            "count": 0,
            "total": 0,
            "data": [],
        }

    raise HTTPException(status_code=404, detail="任务不存在")


async def _stream_task_events(task_type: str, task_id: str) -> AsyncIterator[str]:
    """通过 SSE 推送任务进度（每秒读取一次结果文件）。"""
    output_dir = os.path.join(_BACKEND_DIR, "output")
    result_path = os.path.join(output_dir, f"{task_type}_{task_id}_result.json")
    log_path = os.path.join(output_dir, f"{task_type}_{task_id}.log")

    if not os.path.exists(log_path) and not os.path.exists(result_path):
        err = json.dumps({"error": "任务不存在"}, ensure_ascii=False)
        yield f"event: error\ndata: {err}\n\n"
        return

    last_count = -1
    last_status = None
    # 最多监听 30 分钟；客户端会在页面重新可见时通过轮询重新连接，
    # 因此无需保持过长连接，避免占用服务器和路由器连接资源。
    for _ in range(1800):
        if os.path.exists(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                task_status = payload.get("status", "running")
                count = payload.get("count", 0)
                total = payload.get("total", 0)

                if task_status != last_status or count != last_count:
                    progress = json.dumps(
                        {"status": task_status, "count": count, "total": total},
                        ensure_ascii=False,
                    )
                    yield f"event: progress\ndata: {progress}\n\n"
                    last_status = task_status
                    last_count = count

                if task_status == "done":
                    final = json.dumps(payload, ensure_ascii=False, default=str)
                    yield f"event: completed\ndata: {final}\n\n"
                    return
                if task_status == "error":
                    final = json.dumps(payload, ensure_ascii=False, default=str)
                    yield f"event: error\ndata: {final}\n\n"
                    return
            except Exception:
                # 忽略文件读取的瞬态错误，下轮重试
                pass
        await asyncio.sleep(1)

    err = json.dumps({"error": "SSE 监听超时"}, ensure_ascii=False)
    yield f"event: error\ndata: {err}\n\n"


@router.get("/reprocess-preview/{task_id}/events")
async def reprocess_preview_events(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """新闻重新生成预览任务 SSE 进度流。"""
    return StreamingResponse(
        _stream_task_events("reprocess_preview", task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/refresh-report-preview/{task_id}/events")
async def refresh_report_preview_events(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """刷新正文预览任务 SSE 进度流。"""
    return StreamingResponse(
        _stream_task_events("refresh_report_preview", task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/batch-update")
async def batch_update_news(
    req: BatchUpdateRequest,
    current_user: User = Depends(get_current_user),
):
    """批量更新新闻记录（用于覆盖原新闻）。"""
    if not req.items:
        raise HTTPException(status_code=400, detail="没有要更新的新闻")

    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"DESCRIBE {settings.NEWS_TABLE}")
        allowed_columns = {row[0] for row in cursor.fetchall()}

        updated = 0
        failed = 0
        for item in req.items:
            data = item.model_dump()
            news_id = data.pop("id")
            updates = {
                k: v
                for k, v in data.items()
                if v is not None and k in allowed_columns and k != "id"
            }
            if not updates:
                continue
            fields = [f"{k} = %s" for k in updates.keys()]
            params = list(updates.values())
            params.append(news_id)
            query = f"UPDATE {settings.NEWS_TABLE} SET {', '.join(fields)} WHERE id = %s"
            try:
                cursor.execute(query, params)
                updated += cursor.rowcount
            except Exception as e:
                failed += 1
                print(f"❌ 批量更新失败 ID={news_id}: {e}")

        conn.commit()
        return {
            "success": True,
            "updated": updated,
            "failed": failed,
            "message": f"成功更新 {updated} 条，失败 {failed} 条",
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"批量更新失败: {e}")
    finally:
        cursor.close()
        conn.close()


@router.post("/batch-delete")
async def batch_delete_news(
    req: BatchDeleteRequest,
    current_user: User = Depends(get_current_user),
):
    """批量删除新闻记录。"""
    if not req.ids:
        raise HTTPException(status_code=400, detail="没有要删除的新闻")

    conn = _get_connection()
    cursor = conn.cursor()
    try:
        placeholders = ", ".join(["%s"] * len(req.ids))
        query = f"DELETE FROM {settings.NEWS_TABLE} WHERE id IN ({placeholders})"
        cursor.execute(query, tuple(req.ids))
        conn.commit()
        return {
            "success": True,
            "deleted": cursor.rowcount,
            "message": f"已删除 {cursor.rowcount} 条新闻",
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
    finally:
        cursor.close()
        conn.close()


def _run_crawl_preview_task(task_id: str, cmd: list, output_path: str):
    """Run crawl preview script and write result JSON."""
    log_path = output_path.replace(".json", ".log")
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"[{datetime.now().isoformat()}] start task {task_id}\n")
        log_file.flush()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            log_file.write(proc.stdout or "")
            if proc.stderr:
                log_file.write("\n[STDERR]\n")
                log_file.write(proc.stderr)
            log_file.write(f"\n[EXIT CODE] {proc.returncode}\n")
        except subprocess.TimeoutExpired:
            log_file.write("\n[TIMEOUT] 爬取超过 30 分钟\n")
        except Exception as e:
            log_file.write(f"\n[ERROR] {e}\n")


@router.post("/crawl-preview/start")
async def start_crawl_preview(
    req: CrawlPreviewRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(_require_admin),
):
    """Start a crawl preview task in background (admin only)."""
    script_path = os.path.join(_BACKEND_DIR, "app", "scripts", "run_crawl_preview.py")
    python_bin = sys.executable
    if not os.path.exists(script_path):
        raise HTTPException(status_code=500, detail="爬取预览脚本未找到")

    task_id = str(uuid.uuid4())
    output_dir = os.path.join(_BACKEND_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"crawl_preview_{task_id}.json")

    cmd = [python_bin, script_path, req.vpn_mode]
    if req.start_date:
        cmd.append(req.start_date)
    if req.end_date:
        cmd.append(req.end_date)
    if req.limit:
        cmd.append(str(req.limit))

    background_tasks.add_task(_run_crawl_preview_task, task_id, cmd, output_path)
    return {
        "success": True,
        "task_id": task_id,
        "message": "爬取预览任务已启动",
    }


@router.get("/crawl-preview/{task_id}")
async def get_crawl_preview(
    task_id: str,
    current_user: User = Depends(_require_admin),
):
    """Get crawl preview result by task id (admin only)."""
    output_path = os.path.join(_BACKEND_DIR, "output", f"crawl_preview_{task_id}.json")
    log_path = os.path.join(_BACKEND_DIR, "output", f"crawl_preview_{task_id}.log")

    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "success": True,
                "status": "done",
                "count": len(data),
                "data": data,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析爬取结果失败: {e}")

    if os.path.exists(log_path):
        return {
            "success": True,
            "status": "running",
            "count": 0,
            "data": [],
        }

    raise HTTPException(status_code=404, detail="任务不存在")


@router.get("/analyze-url-platform")
async def analyze_url_platform(
    url: str = Query(..., description="要分析的 URL"),
    current_user: User = Depends(_require_admin),
):
    """Detect source platform and confidence for a URL (admin only)."""
    if not url:
        raise HTTPException(status_code=400, detail="URL 不能为空")

    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname or ""
        hostname = hostname.lower().lstrip("www.")

        cursor.execute(
            "SELECT * FROM url_source_confidence_map WHERE url_pattern = %s",
            (url,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "SELECT * FROM url_source_confidence_map WHERE %s LIKE CONCAT('%%', url_pattern, '%%')",
                (hostname,),
            )
            row = cursor.fetchone()

        if row:
            return {
                "success": True,
                "source_platform": row["source_platform"],
                "confidence_score": row["confidence_score"],
                "known": True,
            }
        return {
            "success": True,
            "source_platform": "其他",
            "confidence_score": 50,
            "known": False,
        }
    finally:
        cursor.close()
        conn.close()


@router.post("/analyze/start")
async def start_analyze(
    req: AnalyzeNewsRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(_require_admin),
):
    """Start a news AI analysis task from database/url/text (admin only)."""
    script_path = os.path.join(_BACKEND_DIR, "app", "scripts", "run_analyze_news.py")
    python_bin = sys.executable
    if not os.path.exists(script_path):
        raise HTTPException(status_code=500, detail="新闻分析脚本未找到")

    task_id = str(uuid.uuid4())
    output_dir = os.path.join(_BACKEND_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"analyze_{task_id}.json")

    cmd = [python_bin, script_path, "--source", req.source]
    if req.start_date:
        cmd.extend(["--start-date", req.start_date])
    if req.end_date:
        cmd.extend(["--end-date", req.end_date])
    if req.date:
        cmd.extend(["--date", req.date])
    if req.url:
        cmd.extend(["--url", req.url])
    if req.text:
        cmd.extend(["--text", req.text])
    if req.title:
        cmd.extend(["--title", req.title])
    if req.source_platform:
        cmd.extend(["--source-platform", req.source_platform])
    if req.confidence_score is not None:
        cmd.extend(["--confidence-score", str(req.confidence_score)])
    if req.limit:
        cmd.extend(["--limit", str(req.limit)])
    cmd.extend(["--output", output_path])

    background_tasks.add_task(_run_crawl_preview_task, task_id, cmd, output_path)
    return {
        "success": True,
        "task_id": task_id,
        "message": "新闻分析任务已启动",
    }


@router.get("/analyze/{task_id}")
async def get_analyze(
    task_id: str,
    current_user: User = Depends(_require_admin),
):
    """Get news analyze result by task id (admin only)."""
    output_path = os.path.join(_BACKEND_DIR, "output", f"analyze_{task_id}.json")
    log_path = os.path.join(_BACKEND_DIR, "output", f"analyze_{task_id}.log")

    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if payload.get("success"):
                data = payload.get("data", [])
                return {
                    "success": True,
                    "status": "done",
                    "count": len(data),
                    "data": data,
                }
            else:
                detail = payload.get("detail") or payload.get("error") or "分析失败"
                raise HTTPException(status_code=500, detail=detail)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析分析结果失败: {e}")

    if os.path.exists(log_path):
        return {
            "success": True,
            "status": "running",
            "count": 0,
            "data": [],
        }

    raise HTTPException(status_code=404, detail="任务不存在")


@router.get("/source-platforms")
async def list_source_platforms(
    current_user: User = Depends(_require_admin),
):
    """Return all distinct source platforms from url_source_confidence_map (admin only)."""
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT DISTINCT source_platform FROM url_source_confidence_map ORDER BY source_platform"
        )
        platforms = [row[0] for row in cursor.fetchall()]
        return {
            "success": True,
            "platforms": platforms,
        }
    finally:
        cursor.close()
        conn.close()


@router.post("/import-selected")
async def import_selected(
    req: ImportSelectedRequest,
    current_user: User = Depends(_require_admin),
):
    """Insert selected preview news items into the database, skipping duplicates (admin only)."""
    items = req.items or []
    if not items:
        raise HTTPException(status_code=400, detail="没有要导入的新闻")

    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"DESCRIBE {settings.NEWS_TABLE}")
        allowed_columns = {row[0] for row in cursor.fetchall()}

        # Existing (date, link) pairs for the requested dates
        dates = sorted(
            {str(item.get("date", "")).strip()[:10] for item in items if item.get("date")}
        )
        existing_pairs: set[tuple[str, str]] = set()
        if dates:
            placeholders = ", ".join(["%s"] * len(dates))
            cursor.execute(
                f"SELECT link, date FROM {settings.NEWS_TABLE} WHERE date IN ({placeholders})",
                dates,
            )
            existing_pairs = {
                (str(row[1]).strip()[:10], row[0] or "") for row in cursor.fetchall()
            }

        inserted = 0
        skipped = 0
        duplicates = []
        for item in items:
            title = item.get("title")
            link = (item.get("link") or "").strip()
            date_raw = item.get("date")
            date = str(date_raw).strip()[:10] if date_raw else None

            if not title:
                skipped += 1
                continue
            # 空链接新闻允许入库，但跳过基于 (date, link) 的重复判断
            if link and date and (date, link) in existing_pairs:
                skipped += 1
                duplicates.append({
                    "title": title,
                    "link": link,
                    "date": date,
                })
                continue

            row = {
                "title": title,
                "link": link,
                "date": date,
                "summary": item.get("summary") or item.get("content") or "",
                "source_platform": item.get("source_platform") or "unknown",
                "confidence_score": (
                    item.get("confidence_score")
                    if item.get("confidence_score") is not None
                    else 50
                ),
                "broad_category": item.get("broad_category") or "其它",
                "category": item.get("category") or "",
                "ai_reprocessed": 0,
            }
            row = {k: v for k, v in row.items() if k in allowed_columns}
            fields = list(row.keys())
            query = (
                f"INSERT INTO {settings.NEWS_TABLE} ("
                f"{', '.join(fields)}) VALUES ({', '.join(['%s'] * len(fields))})"
            )
            cursor.execute(query, list(row.values()))
            inserted += 1
            if date and link:
                existing_pairs.add((date, link))

        conn.commit()
        return {
            "success": True,
            "inserted": inserted,
            "skipped": skipped,
            "duplicates": duplicates,
            "message": f"成功导入 {inserted} 条，跳过 {skipped} 条（含重复/无标题）",
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"导入失败: {e}")
    finally:
        cursor.close()
        conn.close()

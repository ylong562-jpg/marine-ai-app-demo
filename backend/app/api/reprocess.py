# -*- coding: utf-8 -*-
"""
新闻再处理 API（船厂 / 关系网）。

船厂策略复用 app.shipyard_ai 的提取与去重能力；
关系网仍使用 news_engine/news_analysis/backend 下的处理器，
并通过公共 common.crawler 读取缓存的新闻原文，避免重复爬取。
"""
import json
import os
import subprocess
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

# backend 目录（基于本文件位置推导：<repo>/backend）
_BACKEND_DIR = Path(__file__).resolve().parents[2]

# 确保 marine-ai-app 自己的 news_engine 可被导入
_BACKEND_ROOT = str(_BACKEND_DIR / "app" / "news_engine")
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.api.auth import _is_admin
from app.core.config import get_settings
from app.core.security import get_current_user
from app.db.models import User
from app.services.shipyard_import import import_shipyard_rows

settings = get_settings()
router = APIRouter(prefix="/reprocess", tags=["Reprocess"])


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

# 与原 GUI 保持一致的策略名称
STRATEGY_KEYS = ["船厂", "关系网"]

VPN_NEEDED_PLATFORMS = {"Seatrade", "Splash247", "TradeWinds"}


class ReprocessNewsRequest(BaseModel):
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    keyword: Optional[str] = Field(default=None)
    search_scope: Optional[str] = Field(default="title_summary", description="title/summary/title_summary/link/all")
    limit: Optional[int] = Field(default=1000)


class ReprocessExtractRequest(BaseModel):
    ids: List[int] = Field(default_factory=list, description="要处理的新闻 ID 列表")
    strategy: str = Field(default="船厂", description="船厂/关系网")


class ReprocessImportRequest(BaseModel):
    strategy: str = Field(default="船厂")
    items: List[Dict[str, Any]] = Field(default_factory=list)


def _is_vpn_needed(platform: Optional[str]) -> bool:
    return platform in VPN_NEEDED_PLATFORMS


def _get_connection():
    try:
        import mysql.connector
        return mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"数据库连接失败: {e}",
        )


def _fetch_news_by_ids(ids: List[int]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        placeholders = ", ".join(["%s"] * len(ids))
        cursor.execute(
            f"""
            SELECT id, title, link, date, source_platform, summary
            FROM {settings.NEWS_TABLE}
            WHERE id IN ({placeholders})
            ORDER BY date DESC, id DESC
            """,
            ids,
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            date_val = r.get("date")
            date_str = ""
            if date_val:
                if isinstance(date_val, datetime):
                    date_str = date_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val)[:10]
            results.append({
                "id": r["id"],
                "title": r.get("title", ""),
                "link": r.get("link", ""),
                "date": date_str,
                "source_platform": r.get("source_platform", "") or "unknown",
                "summary": r.get("summary", "") or "",
                "needs_vpn": "是" if _is_vpn_needed(r.get("source_platform")) else "否",
            })
        return results
    finally:
        cursor.close()
        conn.close()


def _keyword_matches(row: Dict[str, Any], keyword: str, scope: str) -> bool:
    """根据检索范围判断关键字是否命中。"""
    kw = keyword.lower()
    title = (row.get("title") or "").lower()
    summary = (row.get("summary") or "").lower()
    link = (row.get("link") or "").lower()
    platform = (row.get("source_platform") or "").lower()
    report = (row.get("news_report") or "").lower()

    if scope == "title":
        return kw in title
    if scope == "summary":
        return kw in summary
    if scope == "title_summary":
        return kw in title or kw in summary
    if scope == "link":
        return kw in link
    if scope == "all":
        return kw in title or kw in summary or kw in link or kw in platform or kw in report
    return kw in title or kw in summary


@router.post("/news")
async def load_reprocess_news(
    req: ReprocessNewsRequest,
    current_user: User = Depends(_require_admin),
):
    """加载用于再处理的新闻列表（从 ship_news 读取，支持日期/关键字范围筛选，仅管理员）。"""
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        conditions = []
        params = []
        if req.start_date:
            conditions.append("date >= %s")
            params.append(req.start_date)
        if req.end_date:
            conditions.append("date <= %s")
            params.append(req.end_date)

        keyword = (req.keyword or "").strip()
        scope = req.search_scope or "title_summary"
        # 关键字在 Python 中过滤，若在 SQL 中先 LIMIT 再过滤会漏掉命中记录。
        requires_python_filter = bool(keyword)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"""
            SELECT id, title, link, date, source_platform, summary, news_report, tags, confidence_score
            FROM {settings.NEWS_TABLE}
            {where}
            ORDER BY date DESC, id DESC
        """
        if req.limit and not requires_python_filter:
            query += " LIMIT %s"
            params.append(int(req.limit))

        cursor.execute(query, params)
        rows = cursor.fetchall()

        results = []
        for r in rows:
            title = r.get("title", "") or ""
            summary = r.get("summary", "") or ""
            if requires_python_filter and not _keyword_matches(r, keyword, scope):
                continue
            date_val = r.get("date")
            date_str = ""
            if date_val:
                if isinstance(date_val, datetime):
                    date_str = date_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val)[:10]
            # 从 tags JSON 解析主维度标签，与新闻管理保持一致
            tags_text = r.get("tags") or "{}"
            main_tags_list = []
            try:
                tags_obj = json.loads(tags_text) if isinstance(tags_text, str) else (tags_text or {})
                if isinstance(tags_obj, dict) and isinstance(tags_obj.get("main_tags"), list):
                    main_tags_list = [str(t) for t in tags_obj["main_tags"] if t]
            except Exception:
                pass
            main_tags = ", ".join(main_tags_list) if main_tags_list else "-"

            results.append({
                "id": r["id"],
                "title": title,
                "link": r.get("link", ""),
                "date": date_str,
                "source_platform": r.get("source_platform", "") or "unknown",
                "summary": summary,
                "main_tags": main_tags,
                "confidence_score": r.get("confidence_score") if r.get("confidence_score") is not None else "-",
                "needs_vpn": "是" if _is_vpn_needed(r.get("source_platform")) else "否",
            })
        if req.limit and requires_python_filter:
            results = results[:int(req.limit)]
        return {"success": True, "count": len(results), "data": results}
    finally:
        cursor.close()
        conn.close()


def _run_extract_task(task_id: str, ids: List[int], strategy: str):
    """调用外部 Python 环境执行策略提取，并写入结果文件。"""
    output_dir = str(_BACKEND_DIR / "output")
    os.makedirs(output_dir, exist_ok=True)
    input_path = os.path.join(output_dir, f"reprocess_extract_{task_id}_input.json")
    result_path = os.path.join(output_dir, f"reprocess_extract_{task_id}_result.json")
    log_path = os.path.join(output_dir, f"reprocess_extract_{task_id}.log")

    input_payload = {
        "ids": ids,
        "strategy": strategy,
        "db_config": settings.NEWS_DB_CONFIG,
        "news_table": settings.NEWS_TABLE,
    }
    with open(input_path, "w", encoding="utf-8") as f:
        json.dump(input_payload, f, ensure_ascii=False, default=str)

    script_path = str(_BACKEND_DIR / "app" / "scripts" / "run_reprocess_extract.py")
    python_bin = sys.executable
    cmd = [python_bin, script_path, input_path]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(proc.stdout or "")
            if proc.stderr:
                f.write("\n[STDERR]\n")
                f.write(proc.stderr)
            f.write(f"\n[EXIT CODE] {proc.returncode}\n")
    except subprocess.TimeoutExpired:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n[TIMEOUT] 再处理任务超过 30 分钟\n")
        _write_result(result_path, {"status": "error", "count": 0, "data": [], "error": "任务超时"})
    except Exception as e:
        err = traceback.format_exc()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[ERROR] {e}\n{err}\n")
        _write_result(result_path, {"status": "error", "count": 0, "data": [], "error": str(e)})


def _write_result(path: str, payload: Dict[str, Any]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)


@router.post("/extract/start")
async def start_reprocess_extract(
    req: ReprocessExtractRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(_require_admin),
):
    """启动再处理提取任务（后台运行，仅管理员）。"""
    if req.strategy not in STRATEGY_KEYS:
        raise HTTPException(status_code=400, detail=f"不支持的策略: {req.strategy}")
    if not req.ids:
        raise HTTPException(status_code=400, detail="请选择至少一条新闻")

    task_id = str(uuid.uuid4())
    output_dir = str(_BACKEND_DIR / "output")
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, f"reprocess_extract_{task_id}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] start task {task_id}\n")

    background_tasks.add_task(_run_extract_task, task_id, req.ids, req.strategy)
    return {"success": True, "task_id": task_id, "message": "再处理任务已启动"}


@router.get("/extract/{task_id}")
async def get_reprocess_extract(
    task_id: str,
    current_user: User = Depends(_require_admin),
):
    """查询再处理任务结果（仅管理员）。"""
    output_dir = str(_BACKEND_DIR / "output")
    result_path = os.path.join(output_dir, f"reprocess_extract_{task_id}_result.json")
    log_path = os.path.join(output_dir, f"reprocess_extract_{task_id}.log")

    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {"success": True, **data}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析结果失败: {e}")

    if os.path.exists(log_path):
        return {"success": True, "status": "running", "count": 0, "data": []}

    raise HTTPException(status_code=404, detail="任务不存在")


# ---------------------------------------------------------------------------
# 导入相关
# ---------------------------------------------------------------------------

def _get_shipyard_columns(conn) -> List[str]:
    cursor = conn.cursor()
    try:
        cursor.execute("DESCRIBE shipyard_data")
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()


@router.post("/import")
async def import_reprocess_results(
    req: ReprocessImportRequest,
    current_user: User = Depends(_require_admin),
):
    """将再处理结果导入对应的数据库表（仅管理员）。"""
    if req.strategy not in STRATEGY_KEYS:
        raise HTTPException(status_code=400, detail=f"不支持的策略: {req.strategy}")
    if not req.items:
        raise HTTPException(status_code=400, detail="没有要导入的数据")

    import mysql.connector
    db_config = settings.NEWS_DB_CONFIG
    conn = mysql.connector.connect(**db_config)

    try:
        if req.strategy == "关系网":
            from news_analysis.backend.relationships.database import create_relationships_table, insert_or_update_relationship
            create_relationships_table(conn)
            imported = 0
            skipped = 0
            for item in req.items:
                rel = item.get("_raw", item)
                shipowner = str(rel.get("shipowner", "")).strip()
                shipyard = str(rel.get("shipyard", "")).strip()
                connection = str(rel.get("connection", "")).strip()
                if not shipowner or not shipyard or not connection:
                    skipped += 1
                    continue
                try:
                    insert_or_update_relationship(conn, rel)
                    imported += 1
                except Exception as e:
                    traceback.print_exc()
            return {"success": True, "imported": imported, "skipped": skipped, "message": f"导入完成：成功 {imported} 条，跳过 {skipped} 条"}

        # 船厂：使用与 /shipyards/import 统一的 service
        shipyard_rows = []
        for item in req.items:
            raw = item.get("_raw", item)
            record = dict(raw.get("extracted_data") or raw.get("replacement") or raw or {})
            existing = raw.get("existing_record") or {}

            # 清理 UI-only 字段
            for key in [
                "update_person",
                "update_time",
                "is_verified",
                "create_info",
                "canonical_name",
                "error",
                "file_name",
                "file_type",
                "shipyard_name",
            ]:
                record.pop(key, None)

            # 若与数据库重复，使用已有 id 触发更新；否则新增
            if raw.get("duplicate") and existing.get("id"):
                record["id"] = existing["id"]
            else:
                record.pop("id", None)

            # 保留溯源字段
            record["introduce"] = raw.get("raw_content", "")
            record["url"] = raw.get("url", "")
            record["source_news_link"] = raw.get("source_news_link", "")
            record["source_platform"] = raw.get("source_platform", "")

            shipyard_rows.append(record)

        if not shipyard_rows:
            return {"success": True, "imported": 0, "failed": 0, "message": "没有可导入的船厂数据"}

        result = import_shipyard_rows(shipyard_rows, manager_username=current_user.username)
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("message", "导入失败"))
        return {
            "success": True,
            "imported": result.get("inserted", 0) + result.get("updated", 0),
            "failed": len(result.get("errors", [])),
            "message": result.get("message", "导入完成"),
        }
    finally:
        conn.close()

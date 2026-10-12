"""
Hot focus API endpoints.
Summarizes ship_news from a date range into three hot topics via LLM.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import mysql.connector
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.security import get_current_user
from app.db.models import User

_NEWS_ENGINE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine"
)
if _NEWS_ENGINE_DIR not in sys.path:
    sys.path.insert(0, _NEWS_ENGINE_DIR)

router = APIRouter(prefix="/hot-focus", tags=["Hot Focus"])
settings = get_settings()


class HotFocusDateRangeRequest(BaseModel):
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")


def _get_connection():
    try:
        return mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"数据库连接失败: {e}",
        )


def _get_last_week_range() -> tuple[str, str]:
    today = datetime.now().date()
    # 上周一 = 今天 - (今天星期几 + 7) 天
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return str(last_monday), str(last_sunday)


def _fetch_news(start_date: str, end_date: str, limit: int = 500):
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        query = (
            f"SELECT title, link, date, summary, source_platform "
            f"FROM {settings.NEWS_TABLE} "
            f"WHERE date >= %s AND date <= %s "
            f"ORDER BY date DESC, id DESC LIMIT %s"
        )
        cursor.execute(query, (start_date, end_date, limit))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def _build_news_text(news_list: list[dict]) -> str:
    lines = []
    for i, n in enumerate(news_list, 1):
        title = n.get("title") or ""
        link = n.get("link") or ""
        date = n.get("date") or ""
        summary = n.get("summary") or ""
        source = n.get("source_platform") or ""
        lines.append(
            f"{i}. 标题：{title}\n日期：{date}\n来源：{source}\n链接：{link}\n摘要：{summary}\n"
        )
    return "\n".join(lines)


def _parse_hot_focus_json(raw: str) -> dict:
    """解析 LLM 返回的 JSON，支持去除 markdown 代码块与兜底提取。"""
    text = raw.strip() if raw else ""
    if not text:
        raise ValueError("LLM 返回为空")

    # 去除 markdown 代码块
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 兜底：寻找第一个 { ... } 块
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError("无法从 LLM 返回中提取合法 JSON")


def _normalize_hot_focus(data: any) -> dict:
    """确保返回结构包含 market / international / policy 三个键。"""
    expected = {"market", "international", "policy"}
    if isinstance(data, dict):
        for key in expected:
            if key not in data:
                data[key] = None
        return {k: data[k] for k in expected}
    return {k: None for k in expected}


@router.get("/last-week")
async def get_last_week_hot_focus(
    current_user: User = Depends(get_current_user),
):
    """自动计算上周一至上周日的日期范围并生成热点。"""
    start_date, end_date = _get_last_week_range()
    return await _generate_hot_focus(start_date, end_date)


@router.post("")
async def post_hot_focus(
    req: HotFocusDateRangeRequest,
    current_user: User = Depends(get_current_user),
):
    """根据自定义日期范围生成热点。"""
    return await _generate_hot_focus(req.start_date, req.end_date)


async def _generate_hot_focus(start_date: str, end_date: str):
    news = _fetch_news(start_date, end_date)
    if not news:
        return {
            "success": True,
            "start_date": start_date,
            "end_date": end_date,
            "count": 0,
            "data": {"market": None, "international": None, "policy": None},
            "message": "该日期范围内没有新闻",
        }

    news_text = _build_news_text(news)

    # 延迟导入，避免服务启动时因缺少 API 密钥直接退出
    try:
        from ai_engine import BailianWebAnalyzer
        from utils.prompt import hot_focus_prompt
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"AI 引擎加载失败: {e}",
        )

    try:
        analyzer = BailianWebAnalyzer(region="beijing")
    except SystemExit as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="百炼 API 密钥未配置，无法生成热点",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"AI 分析器初始化失败: {e}",
        )

    result = await analyzer.analyze(
        content=news_text,
        prompt=hot_focus_prompt,
        max_tokens=4000,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"热点生成失败: {result.get('error', '未知错误')}",
        )

    raw_analysis = result.get("analysis", "")
    try:
        data = _parse_hot_focus_json(raw_analysis)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=json.dumps(
                {
                    "error": "LLM 返回结果解析失败",
                    "message": str(e),
                    "raw": raw_analysis,
                },
                ensure_ascii=False,
            ),
        )

    data = _normalize_hot_focus(data)
    return {
        "success": True,
        "start_date": start_date,
        "end_date": end_date,
        "count": len(news),
        "data": data,
    }

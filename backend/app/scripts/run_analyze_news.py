# -*- coding: utf-8 -*-
"""
通用新闻 AI 分析脚本（支持三种数据来源）

Usage:
    # 1. 分析数据库已有新闻
    python run_analyze_news.py --source database --start-date 2026-07-20 --end-date 2026-07-27 --limit 10

    # 2. 分析指定 URL
    python run_analyze_news.py --source url --url https://example.com/news --date 2026-07-27 \
        --source-platform "示例来源" --confidence-score 80

    # 3. 分析用户输入文本
    python run_analyze_news.py --source text --title "标题" --text "新闻正文..." --date 2026-07-27 \
        --source-platform "user:admin" --confidence-score 100
"""

import sys
import os
import asyncio
import argparse
import json
import re
import traceback
from datetime import datetime
from urllib.parse import urlparse

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 导入 news_engine 模块
from ai.ai_classify import re_classify
from ai.ai_news_core_content_extraction import process_news_with_category_prompts
from ai_news_report_creating import generate_news_report_from_existing_data
from ai.ai_engine import BailianWebAnalyzer
from content_extractor import extract_article
from db_config import DB_CONFIG_MYSQL
import mysql.connector


DEFAULT_EXTRACTION_PROMPT = """你是一位专业的新闻内容提取助手。请从以下网页内容中提取新闻信息，并以严格 JSON 格式返回，不要包含任何解释或 Markdown 代码块标记。

要求字段：
- title: 新闻标题（如果没有则根据内容生成一个简洁标题）
- summary: 新闻原文/摘要，保留关键信息，长度控制在 2000 字以内
- date: 新闻发布日期，格式 YYYY-MM-DD（如果无法确定则返回空字符串）

网页内容：
{content}

请只返回 JSON："""


def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG_MYSQL)


def detect_platform_from_url(url: str):
    """根据 URL 查询映射表，返回 (source_platform, confidence_score, is_known)"""
    hostname = urlparse(url).hostname or ""
    hostname = hostname.lower().lstrip("www.")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 先尝试完整 URL 匹配
        cursor.execute(
            "SELECT * FROM url_source_confidence_map WHERE url_pattern = %s",
            (url,),
        )
        row = cursor.fetchone()
        if row:
            return row["source_platform"], row["confidence_score"], True

        # 再按域名匹配
        cursor.execute(
            "SELECT * FROM url_source_confidence_map WHERE %s LIKE CONCAT('%%', url_pattern, '%%')",
            (hostname,),
        )
        row = cursor.fetchone()
        if row:
            return row["source_platform"], row["confidence_score"], True

        return "其他", 50, False
    finally:
        cursor.close()
        conn.close()


def get_or_create_template(url: str):
    """根据 URL 查询/创建 AI 爬虫模板，返回模板字典"""
    hostname = urlparse(url).hostname or ""
    hostname = hostname.lower().lstrip("www.")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 查询现有模板
        cursor.execute(
            "SELECT * FROM ai_crawler_templates WHERE %s LIKE CONCAT('%%', url_pattern, '%%')",
            (hostname,),
        )
        row = cursor.fetchone()
        if row:
            return row

        # 未命中则创建默认模板
        name = f"{hostname} 通用模板"
        cursor.execute(
            "INSERT INTO ai_crawler_templates (name, url_pattern, extraction_prompt, model) "
            "VALUES (%s, %s, %s, %s)",
            (name, hostname, DEFAULT_EXTRACTION_PROMPT, "qwen-plus"),
        )
        conn.commit()
        template_id = cursor.lastrowid
        cursor.execute("SELECT * FROM ai_crawler_templates WHERE id = %s", (template_id,))
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()


async def analyze_with_ai_pipeline(news_data: list) -> list:
    """执行完整 AI 分析流程：分类 -> 提取核心内容 -> 生成报告"""
    if not news_data:
        return []

    news_data = await re_classify(news_data)
    if not news_data:
        return []

    news_data = await process_news_with_category_prompts(news_data)
    if not news_data:
        return []

    news_data = await generate_news_report_from_existing_data(news_data)
    return news_data


def normalize_row(row: dict) -> dict:
    """将数据库行或外部输入统一为可返回前端的字典"""
    tags = row.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = None

    main_tags = ""
    if isinstance(tags, dict):
        main_tags_list = tags.get("main_tags", [])
        if isinstance(main_tags_list, list):
            main_tags = ", ".join(main_tags_list)

    return {
        "title": row.get("title") or "",
        "link": row.get("link") or "",
        "date": str(row.get("date") or "")[:10],
        "summary": row.get("summary") or row.get("content") or "",
        "source_platform": row.get("source_platform") or "unknown",
        "confidence_score": row.get("confidence_score") if row.get("confidence_score") is not None else 50,
        "tags": row.get("tags") or "",
        "main_tags": main_tags,
        "entity": row.get("entity") or "",
        "data": row.get("data") or "",
        "news_report": row.get("news_report") or "",
    }


async def analyze_database(start_date: str, end_date: str, limit: int | None = None):
    """从 ship_news 读取已有新闻并做 AI 分析"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        conditions = ["summary IS NOT NULL AND summary != ''"]
        params = []
        if start_date:
            conditions.append("date >= %s")
            params.append(start_date)
        if end_date:
            conditions.append("date <= %s")
            params.append(end_date)

        query = f"""
            SELECT id, title, link, date, summary, source_platform, confidence_score,
                   tags, entity, data, news_report
            FROM ship_news
            WHERE {' AND '.join(conditions)}
            ORDER BY date DESC, id DESC
        """
        if limit:
            query += " LIMIT %s"
            params.append(limit)

        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    if not rows:
        return []

    # 转换为 AI 流程可处理的字典列表
    news_data = []
    for row in rows:
        news_data.append({
            "id": row["id"],
            "title": row["title"] or "",
            "link": row["link"] or "",
            "date": str(row["date"]) if row["date"] else "",
            "summary": row["summary"] or "",
            "source_platform": row["source_platform"] or "unknown",
            "confidence_score": row["confidence_score"] if row["confidence_score"] is not None else 50,
        })

    results = await analyze_with_ai_pipeline(news_data)
    return [normalize_row(r) for r in results]


async def extract_structured_from_url(url: str, content: str, template: dict):
    """使用 qwen-plus 从网页内容中提取结构化新闻信息"""
    prompt = (template.get("extraction_prompt") or DEFAULT_EXTRACTION_PROMPT).format(content=content[:20000])
    analyzer = BailianWebAnalyzer(region="beijing")
    response = await analyzer.analyze(content=prompt, max_tokens=2000, model="qwen-plus")

    if not response.get("success"):
        raise RuntimeError(f"AI 提取失败: {response.get('error', '未知错误')}")

    raw = response.get("analysis", "")
    # 尝试从响应中解析 JSON
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        parsed = json.loads(raw)
    except Exception as e:
        # 如果解析失败，返回一个兜底结构
        return {
            "title": "URL 解析结果",
            "summary": content[:2000],
            "date": "",
        }

    return {
        "title": parsed.get("title") or "URL 解析结果",
        "summary": parsed.get("summary") or content[:2000],
        "date": parsed.get("date") or "",
    }


async def generate_title_by_ai(content: str) -> str:
    """使用 qwen-plus 从新闻原文中自动生成标题"""
    if not content or len(content.strip()) < 10:
        return "用户输入文本"

    prompt = f"""你是一名专业的新闻编辑。请根据以下新闻原文生成一个简洁、准确、不超过 30 字的中文标题，直接返回标题文本，不要任何解释。

新闻原文：
{content[:5000]}

标题："""
    analyzer = BailianWebAnalyzer(region="beijing")
    response = await analyzer.analyze(content=prompt, max_tokens=100, model="qwen-plus")
    if response.get("success"):
        title = (response.get("analysis") or "").strip()
        # 清理引号和多余内容
        title = title.strip('"\'').split("\n")[0].strip()
        if title and len(title) <= 100:
            return title
    return "用户输入文本"


async def detect_platform_by_ai(url: str, content: str) -> str:
    """使用 qwen-plus 从网页 URL 和内容中识别新闻来源平台名称"""
    hostname = urlparse(url).hostname or ""
    prompt = f"""你是一名新闻来源识别专家。请根据以下网页 URL 和正文内容，判断该新闻最可能来自哪个网站/机构/平台，并只返回一个简洁的中文来源名称（例如：国际船舶网、龙de船人、信德海事网）。

URL: {url}
域名: {hostname}
网页正文（前 3000 字）:
{content[:3000]}

请只返回来源名称，不要任何解释："""
    analyzer = BailianWebAnalyzer(region="beijing")
    response = await analyzer.analyze(content=prompt, max_tokens=100, model="qwen-plus")
    if response.get("success"):
        name = (response.get("analysis") or "").strip()
        # 清理可能的引号和多余内容
        name = name.strip('"\'').split("\n")[0].strip()
        if name and len(name) < 50:
            return name
    return "其他"


def ensure_url_source_mapping(url: str, source_platform: str, confidence_score: int, ai_suggestion: str = ""):
    """将 URL 与来源平台的映射关系写入数据库（如果不存在）"""
    hostname = urlparse(url).hostname or ""
    hostname = hostname.lower().lstrip("www.")
    if not hostname:
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 检查是否已存在该域名的映射
        cursor.execute(
            "SELECT * FROM url_source_confidence_map WHERE %s LIKE CONCAT('%%', url_pattern, '%%')",
            (hostname,),
        )
        row = cursor.fetchone()
        if row:
            return

        # 不存在则插入新映射
        cursor.execute(
            "INSERT INTO url_source_confidence_map (url_pattern, source_platform, confidence_score) "
            "VALUES (%s, %s, %s)",
            (hostname, source_platform, confidence_score),
        )
        conn.commit()
        print(f"🗺️ 已新增 URL 来源映射: {hostname} -> {source_platform} (置信度 {confidence_score})")
        if ai_suggestion and ai_suggestion != source_platform:
            print(f"   AI 推荐来源为: {ai_suggestion}")
    except Exception as e:
        print(f"⚠️ 写入 URL 来源映射失败: {e}")
    finally:
        cursor.close()
        conn.close()


async def analyze_url(url: str, date: str | None, source_platform: str | None, confidence_score: int | None):
    """抓取 URL 并使用 qwen-plus 提取 + AI 分析"""
    if not url:
        raise ValueError("URL 不能为空")

    # 自动检测来源和置信度（如果未提供）
    detected_platform, detected_confidence, is_known = detect_platform_from_url(url)
    if source_platform is None or source_platform == "其他":
        source_platform = detected_platform
    if confidence_score is None:
        confidence_score = detected_confidence

    # 获取或创建爬虫模板
    template = get_or_create_template(url)

    # 抓取网页内容
    fetch_result = await extract_article(url=url, max_length=50000)
    if not fetch_result.get("success"):
        raise RuntimeError(f"网页抓取失败: {fetch_result.get('error', '未知错误')}")

    content = fetch_result.get("content", "")
    if not content or len(content.strip()) < 50:
        raise RuntimeError("网页内容过短，无法分析")

    # 使用 qwen-plus 提取结构化信息
    extracted = await extract_structured_from_url(url, content, template)

    # 使用 AI 分析 URL 与来源的关系（类似船厂船型词表的 AI 匹配）
    ai_suggested_platform = await detect_platform_by_ai(url, content)
    print(f"🔍 AI 识别来源: {ai_suggested_platform}")

    # 如果用户未提供明确来源或映射表中不存在，写入映射表
    if not is_known:
        ensure_url_source_mapping(url, source_platform or ai_suggested_platform, confidence_score or 50, ai_suggested_platform)

    # 使用 AI 从原文生成标题
    summary_text = extracted.get("summary") or content[:2000]
    ai_title = await generate_title_by_ai(summary_text)

    # 构造新闻数据
    item = {
        "title": ai_title,
        "link": url,
        "date": date or extracted.get("date") or datetime.now().strftime("%Y-%m-%d"),
        "summary": summary_text,
        "source_platform": source_platform or ai_suggested_platform or "其他",
        "confidence_score": confidence_score if confidence_score is not None else 50,
    }

    results = await analyze_with_ai_pipeline([item])
    return [normalize_row(r) for r in results]


async def analyze_text(title: str, text: str, date: str | None, source_platform: str, confidence_score: int):
    """分析用户直接输入的文本"""
    if not text:
        raise ValueError("文本内容不能为空")

    # 标题由 AI 根据原文自动生成，忽略用户传入的 title
    ai_title = await generate_title_by_ai(text)

    item = {
        "title": ai_title,
        "link": "",
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "summary": text,
        "source_platform": source_platform,
        "confidence_score": confidence_score,
    }

    results = await analyze_with_ai_pipeline([item])
    return [normalize_row(r) for r in results]


async def main():
    parser = argparse.ArgumentParser(description="通用新闻 AI 分析")
    parser.add_argument("--source", required=True, choices=["database", "url", "text"], help="数据来源")
    parser.add_argument("--start-date", type=str, default=None, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--date", type=str, default=None, help="新闻日期 YYYY-MM-DD")
    parser.add_argument("--url", type=str, default=None, help="要分析的 URL")
    parser.add_argument("--text", type=str, default=None, help="用户输入的文本")
    parser.add_argument("--title", type=str, default=None, help="新闻标题")
    parser.add_argument("--source-platform", type=str, default=None, help="来源平台")
    parser.add_argument("--confidence-score", type=int, default=None, help="置信度")
    parser.add_argument("--limit", type=int, default=None, help="限制条数")
    parser.add_argument("--output", type=str, default=None, help="输出 JSON 文件路径")

    args = parser.parse_args()

    try:
        if args.source == "database":
            results = await analyze_database(args.start_date, args.end_date, args.limit)
        elif args.source == "url":
            results = await analyze_url(
                args.url,
                args.date,
                args.source_platform,
                args.confidence_score,
            )
        elif args.source == "text":
            results = await analyze_text(
                args.title,
                args.text,
                args.date,
                args.source_platform or "user",
                args.confidence_score if args.confidence_score is not None else 100,
            )
        else:
            raise ValueError(f"未知来源: {args.source}")

        output = {
            "success": True,
            "count": len(results),
            "data": results,
        }
    except Exception as e:
        output = {
            "success": False,
            "error": str(e),
            "detail": traceback.format_exc(),
        }

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, default=str)
    else:
        print(json.dumps(output, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())

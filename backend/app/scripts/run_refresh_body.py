# -*- coding: utf-8 -*-
"""
Wrapper to reprocess existing news in the database.
Usage:
    python run_refresh_body.py [days] [limit]
    python run_refresh_body.py --days 7 --limit 100
    python run_refresh_body.py --start-date 2026-07-01 --end-date 2026-07-14
    python run_refresh_body.py --days 7 --keyword "订单" --search-scope title_summary_report
    python run_refresh_body.py --days 7 --steps reclassify,core_content,news_report
"""
import sys
import os
import asyncio
import argparse

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.reprocess_database_news import run_with_config


def run(
    days: int = 7,
    limit: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    keyword: str | None = None,
    search_scope: str = "title_summary_report",
    steps: str | None = None,
    force: bool = False,
):
    config = {
        "days": days if not (start_date or end_date) else None,
        "start_date": start_date,
        "end_date": end_date,
        "process_all": False,
        "limit": limit,
        "concurrent": 3,
        "keyword": keyword,
        "search_scope": search_scope,
        "reprocess_steps": [s.strip() for s in steps.split(",") if s.strip()] if steps else ["reclassify", "core_content", "news_report"],
        "categories": None,
        "force": force,
    }
    updated = asyncio.run(run_with_config(config))
    print(f"\n[REPROCESS] 完成，共更新 {updated} 条新闻")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="重新处理数据库中的新闻")
    parser.add_argument("days", nargs="?", type=int, default=7, help="最近 N 天")
    parser.add_argument("limit", nargs="?", type=int, default=None, help="限制条数")
    parser.add_argument("--start-date", type=str, default=None, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--keyword", type=str, default=None, help="关键字")
    parser.add_argument("--search-scope", type=str, default="title_summary_report", help="检索范围")
    parser.add_argument("--steps", type=str, default=None, help="处理步骤，逗号分隔：reclassify,core_content,news_report")
    parser.add_argument("--force", action="store_true", help="强制重新生成，不跳过已处理的新闻")
    args = parser.parse_args()
    run(
        days=args.days,
        limit=args.limit,
        start_date=args.start_date,
        end_date=args.end_date,
        keyword=args.keyword,
        search_scope=args.search_scope,
        steps=args.steps,
        force=args.force,
    )

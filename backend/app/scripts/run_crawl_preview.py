# -*- coding: utf-8 -*-
"""
Wrapper to crawl news and save JSON preview without importing to database.
Usage:
    python run_crawl_preview.py [vpn_mode] [start_date] [end_date] [limit]
"""
import sys
import os
import asyncio
import json
from io import StringIO

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.news_combine import get_combined_news_with_metadata


def run(
    vpn_mode: str = "all",
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
    output_path: str | None = None,
):
    include_wechat_mp = vpn_mode in ("all", "wechat_only")
    # Suppress noisy crawl prints so stderr stays clean
    old_stdout = sys.stdout
    sys.stdout = StringIO()
    try:
        news = asyncio.run(
            get_combined_news_with_metadata(
                vpn_mode=vpn_mode,
                fetch_content=True,
                include_wechat_mp=include_wechat_mp,
                start_date=start_date,
                end_date=end_date,
            )
        )
    finally:
        sys.stdout = old_stdout
    if limit:
        news = news[:limit]

    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output")
    os.makedirs(output_dir, exist_ok=True)
    if not output_path:
        output_path = os.path.join(output_dir, "crawl_preview_result.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(news, f, ensure_ascii=False, default=str)

    print(f"[CRAWL_PREVIEW] 完成，共 {len(news)} 条，结果保存至 {output_path}")


if __name__ == "__main__":
    vpn_mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    start_date = sys.argv[2] if len(sys.argv) > 2 else None
    end_date = sys.argv[3] if len(sys.argv) > 3 else start_date
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else None
    output_path = sys.argv[5] if len(sys.argv) > 5 else None
    run(
        vpn_mode=vpn_mode,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        output_path=output_path,
    )

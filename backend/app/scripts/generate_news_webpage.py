# -*- coding: utf-8 -*-
"""
Wrapper to call marine-ai-app's own daily_news_generation.main() with a date range.
- Monkey-patches get_date_range_for_news to use [start_date, end_date].
- Monkey-patches save_html_to_desktop so the filename reflects the range.
- Writes a small .meta.json next to the HTML for listing in the UI.
"""
import sys
import os
import json
import re
from datetime import datetime, timedelta

# Add marine-ai-app news_engine root to path
PROJECT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import daily_news_generation


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _format_date(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _build_filename(start: datetime, end: datetime) -> str:
    start_str = _format_date(start)
    end_str = _format_date(end)
    if start_str == end_str:
        return f"每日新闻_{end_str}.html"
    return f"每日新闻_{start_str}_{end_str}.html"


def run(start_date: str | None = None, end_date: str | None = None, vpn_filter: str | None = None):
    start = _parse_date(start_date) if start_date else datetime.now()
    end = _parse_date(end_date) if end_date else start

    if end < start:
        start, end = end, start

    date_list = []
    current = start
    while current <= end:
        date_list.append(current)
        current += timedelta(days=1)

    # Monkey-patch the date range function so daily_news_generation uses our range
    daily_news_generation.get_date_range_for_news = lambda target_date=None: date_list

    # Monkey-patch save function to produce range-based filename
    _original_save = daily_news_generation.save_html_to_desktop

    def _patched_save(html_content, target_date=None):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.getenv('OUTPUT_DIR', os.path.join(base_dir, 'output'))
        news_folder = os.path.join(output_dir, "每日新闻")

        if not os.path.exists(news_folder):
            os.makedirs(news_folder)
            print(f"创建文件夹: {news_folder}")

        filename = _build_filename(start, end)
        filepath = os.path.join(news_folder, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)

        # Write metadata for UI listing
        meta = {
            "filename": filename,
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": end.strftime("%Y-%m-%d"),
            "vpn_filter": vpn_filter,
            "generated_at": datetime.now().isoformat(),
        }
        meta_path = filepath + ".meta.json"
        try:
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] 写入元数据失败: {e}")

        print(f"HTML文档已保存到: {filepath}")
        return filepath

    daily_news_generation.save_html_to_desktop = _patched_save

    try:
        # target_date is only used for print header; use end_date
        daily_news_generation.main(target_date=end_date, vpn_filter=vpn_filter)
    finally:
        daily_news_generation.save_html_to_desktop = _original_save


if __name__ == "__main__":
    start_date = sys.argv[1] if len(sys.argv) > 1 else None
    end_date = sys.argv[2] if len(sys.argv) > 2 else start_date
    vpn_filter = sys.argv[3] if len(sys.argv) > 3 else None
    run(start_date=start_date, end_date=end_date, vpn_filter=vpn_filter)

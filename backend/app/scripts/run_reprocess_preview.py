# -*- coding: utf-8 -*-
"""
Wrapper to run news reprocess preview in background.
Usage:
    python run_reprocess_preview.py <input_json_path>

input_json format:
    {
        "news_ids": [1, 2, 3]
    }

output_json format:
    {
        "status": "running" | "done" | "error",
        "count": 2,
        "total": 3,
        "data": [...],
        "error": "..."
    }
"""
import json
import os
import sys
import traceback
from datetime import datetime

# Add backend news_engine root so we can import the correct reprocess_database_news
_BACKEND_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine")
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import asyncio


def _write_output(path: str, payload: dict):
    """原子性地写入结果文件，避免读取端读到半写入状态。"""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    os.replace(tmp_path, path)


def run(input_path: str, output_path: str, log_path: str):
    completed_items = []
    total = 0

    with open(log_path, "a", encoding="utf-8") as log_file:
        def log(msg):
            log_file.write(f"[{datetime.now().isoformat()}] {msg}\n")
            log_file.flush()

        def progress_callback(item: dict):
            completed_items.append(item)
            _write_output(output_path, {
                "status": "running",
                "count": len(completed_items),
                "total": total,
                "data": completed_items,
                "error": None,
            })
            log(f"进度更新 {len(completed_items)}/{total} ID={item.get('id')}")

        try:
            with open(input_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            news_ids = config.get("news_ids", [])
            total = len(news_ids)
            if not news_ids:
                _write_output(output_path, {
                    "status": "done",
                    "count": 0,
                    "total": 0,
                    "data": [],
                    "error": None,
                })
                log("没有指定新闻 ID")
                return

            # 初始进度文件
            _write_output(output_path, {
                "status": "running",
                "count": 0,
                "total": total,
                "data": [],
                "error": None,
            })
            log(f"启动重新生成预览 news_ids={news_ids} total={total}")

            from core.reprocess_database_news import reprocess_news_preview
            results = asyncio.run(reprocess_news_preview(
                news_ids,
                max_concurrent=config.get("max_concurrent", 8),
                progress_callback=progress_callback,
            ))

            # 最终结果（dedup 后的结果会覆盖中间进度里的 data）
            _write_output(output_path, {
                "status": "done",
                "count": len(results),
                "total": total,
                "data": results,
                "error": None,
            })
            log(f"重新生成预览完成 count={len(results)}/{total}")

        except Exception as e:
            err = traceback.format_exc()
            log(f"ERROR: {e}\n{err}")
            _write_output(output_path, {
                "status": "error",
                "count": len(completed_items),
                "total": total,
                "data": completed_items,
                "error": str(e),
            })


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_reprocess_preview.py <input_json_path>")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = input_path.replace("_input.json", "_result.json")
    log_path = input_path.replace("_input.json", ".log")
    run(input_path, output_path, log_path)

# -*- coding: utf-8 -*-
"""
清理 ship_news.tags 中重复的主维度（main_tags 去重）

历史问题：全局交易合并曾把不同新闻的交易合并到同一篇报道，
导致同一主维度被写入多次（如 ["新造船订单","新造船订单",...]），
前端新闻管理界面会重复展示同一主维度。

本脚本对 tags.main_tags 做保序去重后回写，幂等可重复执行。

Usage:
    python fix_duplicate_main_tags.py [--dry-run]
"""
import sys
import os
import json
import argparse

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import mysql.connector
from db_config import DB_CONFIG_MYSQL


def dedup_preserve_order(items):
    return list(dict.fromkeys(items))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()

    conn = mysql.connector.connect(**DB_CONFIG_MYSQL)
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, tags FROM ship_news WHERE tags IS NOT NULL AND tags != ''")
        rows = cur.fetchall()
        print(f"共扫描 {len(rows)} 行")

        fixed = 0
        affected_ids = []
        for row in rows:
            raw = row["tags"]
            try:
                tags = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(tags, dict):
                continue
            main_tags = tags.get("main_tags")
            if not isinstance(main_tags, list):
                continue
            deduped = dedup_preserve_order(main_tags)
            if deduped == main_tags:
                continue
            tags["main_tags"] = deduped
            affected_ids.append(row["id"])
            fixed += 1
            if not args.dry_run:
                cur.execute(
                    "UPDATE ship_news SET tags = %s WHERE id = %s",
                    (json.dumps(tags, ensure_ascii=False), row["id"]),
                )

        conn.commit() if not args.dry_run else None
        print(f"发现 {fixed} 行存在重复主维度: {affected_ids}")
        print("（dry-run，未写入）" if args.dry_run else f"已回写 {fixed} 行")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

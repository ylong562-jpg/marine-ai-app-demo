# -*- coding: utf-8 -*-
"""
修复微信公众号官方来源置信度

问题：source_platform 中使用了全角冒号 "微信公众号：xxx"，
      导致 is_absolute_truth / calculate_confidence 无法识别官方公众号，
      置信度被错误地打成 80。

用法：
    cd backend/app/news_engine
    python ../../app/scripts/fix_wechat_confidence_scores.py
"""

import sys
import os

# 将 news_engine 目录加入路径，以便导入 confidence_config
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "news_engine"))

import mysql.connector
from confidence_config import calculate_confidence
from utils.db_config import DB_CONFIG_MYSQL


def main():
    conn = mysql.connector.connect(**DB_CONFIG_MYSQL)
    cursor = conn.cursor(dictionary=True)

    # 1) 先查询所有微信公众号记录
    query = """
        SELECT id, source_platform, confidence_score, link
        FROM ship_news
        WHERE source_platform LIKE '微信公众号%'
    """
    cursor.execute(query)
    rows = cursor.fetchall()

    updates = []
    for row in rows:
        new_score = calculate_confidence(
            row["source_platform"],
            1,
            url=row.get("link", "") or ""
        )
        if new_score != row["confidence_score"]:
            updates.append((new_score, row["id"]))

    if not updates:
        print("✅ 没有需要更新的记录")
        cursor.close()
        conn.close()
        return

    print(f"📊 共扫描 {len(rows)} 条记录，其中 {len(updates)} 条需要更新置信度")

    # 2) 批量更新
    update_cursor = conn.cursor()
    try:
        update_cursor.executemany(
            "UPDATE ship_news SET confidence_score = %s WHERE id = %s",
            updates
        )
        conn.commit()
        print(f"✅ 已成功更新 {update_cursor.rowcount} 条记录的置信度")
    except Exception as e:
        conn.rollback()
        print(f"❌ 更新失败: {e}")
        raise
    finally:
        update_cursor.close()
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()

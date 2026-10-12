# -*- coding: utf-8 -*-
"""
清理 MySQL normalized_entity_vocabulary 表中废弃的 entity_type。

规则：
1. 保留的 entity_type 为：shipowner, shipyard, buyer, seller, charterer, owner, company, parties
2. 删除其他所有类型的历史快照记录
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import mysql.connector
from app.core.config import get_settings

ALLOWED_TYPES = {"shipowner", "shipyard", "buyer", "seller", "charterer", "owner", "company", "parties"}


def cleanup():
    settings = get_settings()
    db_config = settings.NEWS_DB_CONFIG
    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor()
    try:
        # 检查表是否存在
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = %s AND table_name = 'normalized_entity_vocabulary'
        """, (db_config["database"],))
        if cur.fetchone()[0] == 0:
            print("表 normalized_entity_vocabulary 不存在，无需清理")
            return

        # 统计清理前
        cur.execute("SELECT entity_type, COUNT(*) FROM normalized_entity_vocabulary GROUP BY entity_type")
        before = dict(cur.fetchall())
        print("清理前统计:", before)

        placeholders = ", ".join(["%s"] * len(ALLOWED_TYPES))
        cur.execute(
            f"DELETE FROM normalized_entity_vocabulary WHERE entity_type NOT IN ({placeholders})",
            tuple(ALLOWED_TYPES),
        )
        deleted = cur.rowcount
        conn.commit()
        print(f"删除非允许类型记录: {deleted} 条")

        # 统计清理后
        cur.execute("SELECT entity_type, COUNT(*) FROM normalized_entity_vocabulary GROUP BY entity_type")
        after = dict(cur.fetchall())
        print("清理后统计:", after)
        print("MySQL 词表清理完成")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    cleanup()

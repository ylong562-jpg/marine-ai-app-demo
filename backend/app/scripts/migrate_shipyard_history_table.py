# -*- coding: utf-8 -*-
"""
迁移脚本：创建 shipyard_update_history 表，用于记录船厂字段级修改历史。

运行方式：
    cd <repo>/backend
    source .venv/bin/activate
    python app/scripts/migrate_shipyard_history_table.py
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings

DB_CONFIG = get_settings().NEWS_DB_CONFIG
TABLE = "shipyard_update_history"


def main():
    import mysql.connector

    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                shipyard_id BIGINT NOT NULL,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_person VARCHAR(100) NOT NULL,
                changes JSON NOT NULL,
                INDEX idx_shipyard_id (shipyard_id),
                INDEX idx_update_time (update_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        conn.commit()
        print(f"[DB] 表 {TABLE} 已创建或已存在")
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()

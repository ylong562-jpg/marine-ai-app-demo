# -*- coding: utf-8 -*-
"""
词表同步脚本

每天早上 8 点将本地词表同步到 Time_Series 数据库，作为历史快照存储：
- entity_registry.db -> normalized_entity_vocabulary（含 shipyard 记录）
- ship_type_taxonomy.json -> normalized_shiptype_vocabulary

normalized_shipyard_vocabulary 已弃用，船厂映射统一通过 normalized_entity_vocabulary
中 entity_type='shipyard' 的记录获取。

本地文件继续保留作为缓存和事实来源，数据库表仅用于后续查询/回溯。
"""

import sys
import os
import json
import sqlite3
from datetime import datetime, date
from typing import Any

# 确保能导入 app/news_engine 下的模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_ENGINE_ROOT = os.path.join(PROJECT_ROOT, "news_engine")
if NEWS_ENGINE_ROOT not in sys.path:
    sys.path.insert(0, NEWS_ENGINE_ROOT)

import mysql.connector
from utils.db_config import DB_CONFIG_MYSQL

DATA_DIR = os.path.join(NEWS_ENGINE_ROOT, "data")

TABLE_ENTITY = "normalized_entity_vocabulary"
TABLE_SHIPTYPE = "normalized_shiptype_vocabulary"

CREATE_TABLE_SQL = {
    TABLE_ENTITY: f"""
    CREATE TABLE IF NOT EXISTS {TABLE_ENTITY} (
        id INT AUTO_INCREMENT PRIMARY KEY,
        snapshot_date DATE NOT NULL,
        entity_id INT,
        entity_type VARCHAR(100) NOT NULL,
        canonical_name VARCHAR(500) NOT NULL,
        english_name VARCHAR(500),
        aliases JSON,
        frequency INT DEFAULT 1,
        created_at DATETIME,
        last_seen DATETIME,
        code VARCHAR(100),
        specificity VARCHAR(20),
        synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_snapshot_entity (snapshot_date, entity_type, canonical_name),
        KEY idx_code (code),
        KEY idx_snapshot (snapshot_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,

    TABLE_SHIPTYPE: f"""
    CREATE TABLE IF NOT EXISTS {TABLE_SHIPTYPE} (
        id INT AUTO_INCREMENT PRIMARY KEY,
        snapshot_date DATE NOT NULL,
        canonical_name VARCHAR(200) NOT NULL,
        aliases JSON,
        category VARCHAR(200),
        parent VARCHAR(200),
        level INT,
        level_1 VARCHAR(200),
        level_2 VARCHAR(200),
        level_3 VARCHAR(200),
        path_json JSON,
        size_info VARCHAR(100),
        source_info VARCHAR(100),
        synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_snapshot_name (snapshot_date, canonical_name),
        KEY idx_snapshot (snapshot_date),
        KEY idx_level (level)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
}


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_datetime(value: Any) -> Any:
    """将可能的时间字符串转为 datetime 对象；无法解析返回 NULL。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def ensure_tables(cursor) -> None:
    for table_name, sql in CREATE_TABLE_SQL.items():
        cursor.execute(sql)
        print(f"[SYNC] 表已确保存在: {table_name}")

    # 兼容旧表结构：缺少的列自动补上
    cursor.execute(f"SHOW COLUMNS FROM {TABLE_ENTITY}")
    cols = {row[0] for row in cursor.fetchall()}
    for col, col_def in (
        ("english_name", "english_name VARCHAR(500)"),
        ("specificity", "specificity VARCHAR(20)"),
    ):
        if col not in cols:
            cursor.execute(f"ALTER TABLE {TABLE_ENTITY} ADD COLUMN {col_def}")
            print(f"[SYNC] 已补列: {TABLE_ENTITY}.{col}")


def sync_entity_vocabulary(cursor, snapshot_date: date) -> int:
    db_path = os.path.join(DATA_DIR, "entity_registry.db")
    if not os.path.exists(db_path):
        print(f"[SYNC] 跳过 entity：文件不存在 {db_path}")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM entities").fetchall()
    conn.close()

    insert_sql = f"""
    INSERT INTO {TABLE_ENTITY}
    (snapshot_date, entity_id, entity_type, canonical_name, english_name, aliases, frequency, created_at, last_seen, code, specificity)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        english_name = VALUES(english_name),
        aliases = VALUES(aliases),
        frequency = VALUES(frequency),
        created_at = VALUES(created_at),
        last_seen = VALUES(last_seen),
        code = VALUES(code),
        specificity = VALUES(specificity),
        synced_at = VALUES(synced_at)
    """

    count = 0
    for row in rows:
        aliases = row["aliases"]
        try:
            aliases_json = json.dumps(json.loads(aliases), ensure_ascii=False) if aliases else None
        except Exception:
            aliases_json = json.dumps([aliases], ensure_ascii=False) if aliases else None

        cursor.execute(
            insert_sql,
            (
                snapshot_date,
                row["id"],
                row["entity_type"],
                row["canonical_name"],
                row["english_name"] if "english_name" in row.keys() else None,
                aliases_json,
                row["frequency"],
                _parse_datetime(row["created_at"]),
                _parse_datetime(row["last_seen"]),
                row["code"],
                row["specificity"],
            ),
        )
        count += 1

    print(f"[SYNC] entity 同步完成: {count} 条 (snapshot={snapshot_date})")
    return count


def sync_shipyard_vocabulary(cursor, snapshot_date: date) -> int:
    db_path = os.path.join(DATA_DIR, "entity_registry.db")
    if not os.path.exists(db_path):
        print(f"[SYNC] 跳过 shipyard：文件不存在 {db_path}")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT canonical_name, aliases FROM entities WHERE entity_type = 'shipyard'"
    ).fetchall()
    conn.close()

    insert_sql = f"""
    INSERT INTO {TABLE_SHIPYARD}
    (snapshot_date, alias_name, canonical_name)
    VALUES (%s, %s, %s)
    ON DUPLICATE KEY UPDATE
        canonical_name = VALUES(canonical_name),
        synced_at = VALUES(synced_at)
    """

    count = 0
    for row in rows:
        canonical = row["canonical_name"]
        aliases = row["aliases"]
        try:
            alias_list = json.loads(aliases) if aliases else []
        except Exception:
            alias_list = [aliases] if aliases else []
        # 规范名自身也作为别名写入，保证所有映射都被覆盖
        all_names = [canonical] + [a for a in alias_list if a and a != canonical]
        for name in all_names:
            cursor.execute(insert_sql, (snapshot_date, name, canonical))
            count += 1

    print(f"[SYNC] shipyard 同步完成: {count} 条 (snapshot={snapshot_date})")
    return count


def sync_shiptype_vocabulary(cursor, snapshot_date: date) -> int:
    json_path = os.path.join(DATA_DIR, "ship_type_taxonomy.json")
    if not os.path.exists(json_path):
        print(f"[SYNC] 跳过 shiptype：文件不存在 {json_path}")
        return 0

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("entries", []) if isinstance(data, dict) else data

    insert_sql = f"""
    INSERT INTO {TABLE_SHIPTYPE}
    (snapshot_date, canonical_name, aliases, category, parent, level, level_1, level_2, level_3, path_json, size_info, source_info)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        aliases = VALUES(aliases),
        category = VALUES(category),
        parent = VALUES(parent),
        level = VALUES(level),
        level_1 = VALUES(level_1),
        level_2 = VALUES(level_2),
        level_3 = VALUES(level_3),
        path_json = VALUES(path_json),
        size_info = VALUES(size_info),
        source_info = VALUES(source_info),
        synced_at = VALUES(synced_at)
    """

    count = 0
    for entry in entries:
        cursor.execute(
            insert_sql,
            (
                snapshot_date,
                entry.get("canonical_name"),
                json.dumps(entry.get("aliases", []), ensure_ascii=False) if entry.get("aliases") else None,
                entry.get("category"),
                entry.get("parent"),
                entry.get("level"),
                entry.get("level_1"),
                entry.get("level_2"),
                entry.get("level_3"),
                json.dumps(entry.get("path", []), ensure_ascii=False) if entry.get("path") else None,
                entry.get("size", ""),
                entry.get("source", ""),
            ),
        )
        count += 1

    print(f"[SYNC] shiptype 同步完成: {count} 条 (snapshot={snapshot_date})")
    return count


def run_sync(snapshot_date: date | None = None) -> dict:
    snapshot_date = snapshot_date or date.today()

    print(f"[SYNC] 开始同步词表到 Time_Series 数据库，snapshot={snapshot_date}")
    conn = mysql.connector.connect(**DB_CONFIG_MYSQL)
    cursor = conn.cursor()

    try:
        ensure_tables(cursor)

        counts = {
            TABLE_ENTITY: sync_entity_vocabulary(cursor, snapshot_date),
            TABLE_SHIPTYPE: sync_shiptype_vocabulary(cursor, snapshot_date),
        }

        conn.commit()
        print(f"[SYNC] 全部同步成功: {counts}")
        return counts
    except Exception as e:
        conn.rollback()
        print(f"[SYNC] 同步失败: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    snapshot = None
    if len(sys.argv) > 1:
        try:
            snapshot = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"[SYNC] 日期参数格式错误: {sys.argv[1]}，应为 YYYY-MM-DD")
            sys.exit(1)
    run_sync(snapshot)

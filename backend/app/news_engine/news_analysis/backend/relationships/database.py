# -*- coding: utf-8 -*-
"""
news_analysis 后端 - 公司关系数据管理

管理 ship_relationships 表：存储船东、船厂、其他公司之间的合作关系。
"""

import sys
import os

if __name__ == "__main__" and not getattr(sys, "frozen", False):
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

import json
from typing import Dict, List, Optional, Any

import mysql.connector

from news_analysis.backend.common.config import DEFAULT_DB_CONFIG


RELATIONSHIP_TABLE = "ship_relationships"


def connect_db(db_config: Optional[Dict[str, Any]] = None):
    """连接 MySQL 数据库。"""
    config = db_config or DEFAULT_DB_CONFIG
    return mysql.connector.connect(**config)


def create_relationships_table(conn):
    """手动创建 ship_relationships 表。建议在确认需要该表后再调用。"""
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {RELATIONSHIP_TABLE} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                shipowner VARCHAR(255),
                shipyard VARCHAR(255),
                connection VARCHAR(50),
                date VARCHAR(50),
                confidence_score VARCHAR(50),
                source_url JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_shipowner (shipowner),
                INDEX idx_shipyard (shipyard),
                INDEX idx_connection (connection)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()
    finally:
        cursor.close()


def table_exists(conn) -> bool:
    """检查 ship_relationships 表是否已存在。"""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = %s
            LIMIT 1
        """, (RELATIONSHIP_TABLE,))
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def fetch_relationships(
    conn,
    keyword: Optional[str] = None,
    connection: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """查询关系记录。"""
    cursor = conn.cursor(dictionary=True)
    try:
        conditions = []
        params = []
        if keyword:
            like = f"%{keyword}%"
            conditions.append("(shipowner LIKE %s OR shipyard LIKE %s)")
            params.extend([like, like])
        if connection:
            conditions.append("connection = %s")
            params.append(connection)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"SELECT * FROM {RELATIONSHIP_TABLE}{where} ORDER BY id DESC"
        if limit:
            query += " LIMIT %s"
            params.append(int(limit))

        cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def find_existing_relationship(
    conn,
    shipowner: str,
    shipyard: str,
    connection: str,
) -> Optional[Dict[str, Any]]:
    """根据船东、船厂、合作方式查找已存在的关系记录。"""
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT * FROM {RELATIONSHIP_TABLE}
            WHERE shipowner = %s AND shipyard = %s AND connection = %s
            LIMIT 1
            """,
            (shipowner, shipyard, connection),
        )
        return cursor.fetchone()
    finally:
        cursor.close()


def _parse_confidence(score: Any) -> float:
    """将 confidence_score 解析为浮点数，失败返回 0。"""
    if score is None or score == "":
        return 0.0
    try:
        return float(score)
    except (ValueError, TypeError):
        return 0.0


def _merge_sources(existing_sources: Any, new_source: Dict[str, Any]) -> List[Dict[str, Any]]:
    """合并来源列表，避免重复 URL。"""
    sources = []
    if existing_sources:
        if isinstance(existing_sources, str):
            try:
                sources = json.loads(existing_sources)
            except json.JSONDecodeError:
                sources = []
        elif isinstance(existing_sources, list):
            sources = existing_sources

    if not isinstance(sources, list):
        sources = []

    # 去重：相同 url 的来源保留最新一次
    url_map = {s.get("url", ""): s for s in sources if isinstance(s, dict)}
    url_map[new_source.get("url", "")] = new_source
    return list(url_map.values())


def insert_or_update_relationship(
    conn,
    relationship: Dict[str, Any],
) -> int:
    """
    插入或更新关系记录。
    如果存在相同 shipowner + shipyard + connection 的记录，
    则合并 source_url 并将 confidence_score 更新为最高值。

    注意：调用前请确保 ship_relationships 表已创建，否则抛出 RuntimeError。

    Returns:
        记录 ID
    """
    if not table_exists(conn):
        raise RuntimeError(
            f"表 {RELATIONSHIP_TABLE} 不存在。"
            f"请先在数据库中执行 create_relationships_table(conn) 创建该表。"
        )

    shipowner = str(relationship.get("shipowner", "")).strip()
    shipyard = str(relationship.get("shipyard", "")).strip()
    connection = str(relationship.get("connection", "")).strip()
    date = str(relationship.get("date", "")).strip()
    new_score = _parse_confidence(relationship.get("confidence_score"))
    new_source = {
        "url": str(relationship.get("source_url", "")),
        "date": date,
        "confidence_score": new_score,
    }

    existing = find_existing_relationship(conn, shipowner, shipyard, connection)

    cursor = conn.cursor(dictionary=True)
    try:
        if existing:
            # 更新：合并来源，confidence_score 取最高
            existing_id = existing["id"]
            existing_score = _parse_confidence(existing.get("confidence_score"))
            final_score = max(existing_score, new_score)
            merged_sources = _merge_sources(existing.get("source_url"), new_source)

            cursor.execute(
                f"""
                UPDATE {RELATIONSHIP_TABLE}
                SET date = %s,
                    confidence_score = %s,
                    source_url = %s
                WHERE id = %s
                """,
                (
                    date or existing.get("date", ""),
                    str(final_score),
                    json.dumps(merged_sources, ensure_ascii=False),
                    existing_id,
                ),
            )
            conn.commit()
            return existing_id
        else:
            # 新增
            sources = [new_source]
            cursor.execute(
                f"""
                INSERT INTO {RELATIONSHIP_TABLE}
                (shipowner, shipyard, connection, date, confidence_score, source_url)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    shipowner,
                    shipyard,
                    connection,
                    date,
                    str(new_score),
                    json.dumps(sources, ensure_ascii=False),
                ),
            )
            conn.commit()
            return cursor.lastrowid
    finally:
        cursor.close()


def update_relationship(conn, relationship_id: int, updates: Dict[str, Any]) -> bool:
    """更新单条关系记录。"""
    allowed_columns = {"shipowner", "shipyard", "connection", "date", "confidence_score"}
    fields = []
    params = []
    for key, value in updates.items():
        if key not in allowed_columns or key == "id":
            continue
        fields.append(f"{key} = %s")
        params.append(value)

    if not fields:
        return False

    params.append(relationship_id)
    query = f"UPDATE {RELATIONSHIP_TABLE} SET {', '.join(fields)} WHERE id = %s"

    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()

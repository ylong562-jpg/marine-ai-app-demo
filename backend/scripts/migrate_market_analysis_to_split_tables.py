# -*- coding: utf-8 -*-
"""
一次性迁移脚本：将 market_analysis_transactions 宽表数据
按 category 拆分到三张独立表。

运行方式（在项目根目录或 backend 目录下）：
    cd <repo>/backend
    python -m scripts.migrate_market_analysis_to_split_tables

或直接执行：
    python <repo>/backend/scripts/migrate_market_analysis_to_split_tables.py
"""
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# 将 backend 加入路径，以便导入 app 包
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import mysql.connector

from app.core.config import get_settings
from app.services.market_analysis_service import (
    _ensure_market_analysis_table,
    _get_market_table,
    _MARKET_TABLES,
)

SOURCE_TABLE = "market_analysis_transactions"


def _load_source_rows(conn) -> List[Dict[str, Any]]:
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT * FROM `{SOURCE_TABLE}` ORDER BY id")
        return cursor.fetchall()
    finally:
        cursor.close()


def _migrate() -> None:
    settings = get_settings()
    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    try:
        # 确保新表已创建
        _ensure_market_analysis_table(conn)

        # 检查原表是否存在
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                (SOURCE_TABLE,),
            )
            source_exists = cursor.fetchone()[0] > 0
        finally:
            cursor.close()

        if not source_exists:
            print(f"原表 {SOURCE_TABLE} 不存在，无需迁移。新表已创建。")
            return

        rows = _load_source_rows(conn)
        if not rows:
            print(f"原表 {SOURCE_TABLE} 为空，无需迁移。新表已创建。")
            return

        # 按类别分组
        grouped: Dict[str, List[Dict[str, Any]]] = {
            cat: [] for cat in _MARKET_TABLES.keys()
        }
        skipped = 0
        for row in rows:
            category = row.get("category")
            if category not in grouped:
                skipped += 1
                continue
            grouped[category].append(row)

        columns = [
            "news_id",
            "news_title",
            "news_link",
            "news_date",
            "news_summary",
            "source_platform",
            "category_key",
            "`date`",
            "ship_type",
            "quantity",
            "quantity_int",
            "specs",
            "trade_type",
            "shipowner",
            "shipyard",
            "buyer",
            "seller",
            "charterer",
            "owner",
            "delivery",
            "power",
            "charter_type",
            "original_price",
            "currency",
            "cny_min",
            "cny_max",
            "cny_display",
            "is_rate",
            "confidence_score",
            "raw_transaction",
            "tx_hash",
        ]
        placeholders = ", ".join(["%s"] * len(columns))

        for category, table_name in _MARKET_TABLES.items():
            cat_rows = grouped.get(category, [])
            if not cat_rows:
                print(f"类别 {category}（{table_name}）：无数据")
                continue

            values = []
            for row in cat_rows:
                raw_tx = row.get("raw_transaction")
                if isinstance(raw_tx, dict):
                    raw_tx = json.dumps(raw_tx, ensure_ascii=False)
                elif raw_tx is None:
                    raw_tx = None
                else:
                    raw_tx = str(raw_tx)

                value_tuple = (
                    row.get("news_id"),
                    row.get("news_title"),
                    row.get("news_link"),
                    row.get("news_date"),
                    row.get("news_summary"),
                    row.get("source_platform"),
                    row.get("category_key"),
                    row.get("date"),
                    row.get("ship_type"),
                    row.get("quantity"),
                    row.get("quantity_int"),
                    row.get("specs"),
                    row.get("trade_type"),
                    row.get("shipowner"),
                    row.get("shipyard"),
                    row.get("buyer"),
                    row.get("seller"),
                    row.get("charterer"),
                    row.get("owner"),
                    row.get("delivery"),
                    row.get("power"),
                    row.get("charter_type"),
                    row.get("original_price"),
                    row.get("currency"),
                    row.get("cny_min"),
                    row.get("cny_max"),
                    row.get("cny_display"),
                    1 if row.get("is_rate") else 0,
                    row.get("confidence_score"),
                    raw_tx,
                    row.get("tx_hash"),
                )
                values.append(value_tuple)

            query = f"""
                INSERT IGNORE INTO `{table_name}` ({', '.join(columns)})
                VALUES ({placeholders})
            """
            cursor = conn.cursor()
            try:
                cursor.executemany(query, values)
                conn.commit()
                inserted = cursor.rowcount
                print(
                    f"类别 {category}（{table_name}）："
                    f"读取 {len(cat_rows)} 条，写入 {inserted} 条"
                )
            finally:
                cursor.close()

        if skipped:
            print(f"跳过未知类别记录：{skipped} 条")

        print("迁移完成。原表未删除，可手动确认后清理。")
    finally:
        conn.close()


if __name__ == "__main__":
    _migrate()

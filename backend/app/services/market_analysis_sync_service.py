# -*- coding: utf-8 -*-
"""
市场分析数据同步服务

将 ship_news 中已提取（或尚未提取）的市场交易数据按主标签分别写入三张独立表：
- market_analysis_newbuilding（新造船订单）
- market_analysis_sale（买卖船）
- market_analysis_charter（租船）

供市场分析页面直接读取，避免每次请求都重新解析原始新闻 data 字段。
"""
import asyncio
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional

import mysql.connector

from app.core.config import get_settings
from app.services.market_analysis_service import (
    _CATEGORY_MAP,
    _ensure_market_analysis_table,
    _parse_json_loose,
    _transaction_to_record,
)

# 旧表名，保留仅作参考；新逻辑按类别写入 _MARKET_TABLES 对应表
_MARKET_ANALYSIS_TABLE = "market_analysis_transactions"


def _is_data_empty(row: Dict[str, Any]) -> bool:
    """判断新闻 data 字段是否为空。"""
    data = row.get("data")
    if data is None:
        return True
    if isinstance(data, str):
        text = data.strip()
        if not text or text in ("null", "None", "{}", "[]"):
            return True
    parsed = _parse_json_loose(data)
    if parsed is None:
        return True
    if isinstance(parsed, (dict, list)):
        return len(parsed) == 0
    return False


_reprocess_single_news_fn: Optional[Any] = None


def _get_reprocess_single_news() -> Any:
    """动态导入并缓存 news_engine 中的单条新闻 AI 重新处理函数。"""
    global _reprocess_single_news_fn
    if _reprocess_single_news_fn is not None:
        return _reprocess_single_news_fn

    news_engine_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "news_engine",
    )
    if news_engine_dir not in sys.path:
        sys.path.insert(0, news_engine_dir)
    from reprocess_database_news import reprocess_single_news_by_id

    _reprocess_single_news_fn = reprocess_single_news_by_id
    return _reprocess_single_news_fn


def _has_price_in_data(data_obj: Any, category: str) -> bool:
    """递归检查 data 对象中是否存在有效 price / money 字段。"""
    price_fields = ("price",) if category == "新造船订单" else ("money", "price")
    if isinstance(data_obj, dict):
        for field in price_fields:
            value = data_obj.get(field)
            if value is not None and str(value).strip() not in ("", "null", "None"):
                return True
        for v in data_obj.values():
            if _has_price_in_data(v, category):
                return True
    elif isinstance(data_obj, list):
        for item in data_obj:
            if _has_price_in_data(item, category):
                return True
    elif isinstance(data_obj, str):
        parsed = _parse_json_loose(data_obj)
        if parsed is not None and parsed != data_obj:
            return _has_price_in_data(parsed, category)
    return False


def _extract_transactions_for_category(
    data_obj: Any, category: str
) -> List[Dict[str, Any]]:
    """从 data 中只提取与指定主维度匹配的交易列表。"""
    if not data_obj:
        return []

    if isinstance(data_obj, list):
        # 分组格式：列表项含 main_tag
        if any(isinstance(item, dict) and item.get("main_tag") for item in data_obj):
            transactions = []
            for item in data_obj:
                if not isinstance(item, dict):
                    continue
                main_tag = item.get("main_tag")
                if main_tag != category:
                    continue
                inner = item.get("data")
                if isinstance(inner, str):
                    inner = _parse_json_loose(inner)
                if inner is not None:
                    transactions.extend(_extract_transactions_for_category(inner, category))
                elif isinstance(item.get("transactions"), list):
                    transactions.extend(item["transactions"])
            return transactions

        # 非分组格式：列表项直接是交易包装或交易本身
        transactions = []
        for item in data_obj:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("transactions"), list):
                transactions.extend(item["transactions"])
            else:
                transactions.append(item)
        return transactions

    if isinstance(data_obj, dict):
        txs = data_obj.get("transactions")
        if isinstance(txs, list):
            return txs
        return [data_obj]

    return []


def _extract_records_from_news_item(
    news_item: Dict[str, Any], category: str
) -> List[Dict[str, Any]]:
    """从单条新闻对象中提取并归一化指定类别的交易记录。"""
    data_obj = _parse_json_loose(news_item.get("data"))
    transactions = _extract_transactions_for_category(data_obj, category)
    records = []
    for tx in transactions:
        record = _transaction_to_record(news_item, tx, category)
        if record:
            records.append(record)
    return records


def _compute_tx_hash(record: Dict[str, Any]) -> str:
    """为交易记录生成去重/唯一标识哈希。"""
    key_obj = {
        "news_id": record.get("news_id"),
        "category": record.get("category"),
        "date": record.get("date"),
        "ship_type": record.get("ship_type"),
        "quantity": record.get("quantity"),
        "shipowner": record.get("shipowner"),
        "shipyard": record.get("shipyard"),
        "buyer": record.get("buyer"),
        "seller": record.get("seller"),
        "charterer": record.get("charterer"),
        "owner": record.get("owner"),
        "original_price": record.get("original_price"),
    }
    key_str = json.dumps(key_obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key_str.encode("utf-8")).hexdigest()


def _delete_existing_rows(
    conn,
    category: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> int:
    """全量重建时删除指定类别表在日期范围内的已有记录。"""
    from app.services.market_analysis_service import _get_market_table

    table_name = _get_market_table(category)
    conditions: List[str] = []
    params: List[Any] = []
    if start_date:
        conditions.append("news_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("news_date <= %s")
        params.append(end_date)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        DELETE FROM `{table_name}`
        {where_clause}
    """
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        return cursor.rowcount
    finally:
        cursor.close()


def _fetch_candidate_news(
    conn,
    category: str,
    start_date: Optional[str],
    end_date: Optional[str],
    full_rebuild: bool,
) -> List[Dict[str, Any]]:
    """获取待处理的新闻列表。非全量重建时跳过已写入该类别交易表的新闻。"""
    conditions = ["JSON_CONTAINS(NULLIF(tags, ''), %s, '$.main_tags')"]
    params: List[Any] = [json.dumps(category)]

    if start_date:
        conditions.append("date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("date <= %s")
        params.append(end_date)

    if not full_rebuild:
        from app.services.market_analysis_service import _get_market_table

        table_name = _get_market_table(category)
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM `{table_name}` m "
            "WHERE m.news_id = ship_news.id)"
        )

    query = f"""
        SELECT
            id, title, link, date, summary, source_platform,
            tags, data, news_report, confidence_score
        FROM ship_news
        WHERE {" AND ".join(conditions)}
        ORDER BY date DESC, id DESC
    """
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def _insert_records(
    conn,
    records: List[Dict[str, Any]],
    news_item: Dict[str, Any],
    category: str,
    category_key: str,
) -> int:
    """将交易记录批量写入市场分析表，按 tx_hash 去重。"""
    if not records:
        return 0

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
        "spec_value",
        "spec_unit",
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

    news_id = news_item.get("id")
    news_title = news_item.get("title")
    news_link = news_item.get("link")
    news_date = news_item.get("date")
    if hasattr(news_date, "strftime"):
        news_date = news_date.strftime("%Y-%m-%d")
    news_summary = news_item.get("summary")
    source_platform = news_item.get("source_platform")
    confidence_score = news_item.get("confidence_score")

    from app.services.market_analysis_service import _get_market_table

    table_name = _get_market_table(category)

    values = []
    for record in records:
        tx = record.get("_raw_transaction") or {}
        tx_hash = _compute_tx_hash(record)
        row = (
            news_id,
            news_title,
            news_link,
            news_date,
            news_summary,
            source_platform,
            category_key,
            record.get("date"),
            record.get("ship_type"),
            record.get("quantity"),
            None,
            record.get("specs"),
            record.get("spec_value"),
            record.get("spec_unit"),
            record.get("trade_type"),
            record.get("shipowner"),
            record.get("shipyard"),
            record.get("buyer"),
            record.get("seller"),
            record.get("charterer"),
            record.get("owner"),
            record.get("delivery"),
            record.get("power"),
            record.get("charter_type"),
            record.get("original_price"),
            record.get("currency"),
            record.get("cny_min"),
            record.get("cny_max"),
            record.get("cny_display"),
            1 if record.get("is_rate") else 0,
            confidence_score,
            json.dumps(tx, ensure_ascii=False) if tx else None,
            tx_hash,
        )
        values.append(row)

    placeholders = ", ".join(["%s"] * len(columns))
    query = f"""
        INSERT IGNORE INTO `{table_name}` ({', '.join(columns)})
        VALUES ({placeholders})
    """
    cursor = conn.cursor()
    try:
        cursor.executemany(query, values)
        conn.commit()
        return cursor.rowcount
    finally:
        cursor.close()


async def _ai_process_news(news_id: int, semaphore: asyncio.Semaphore) -> None:
    """调用 news_engine 的 AI 流程重新处理单条新闻。"""
    async with semaphore:
        reprocess = _get_reprocess_single_news()
        await reprocess(news_id)


def _fetch_news_by_id(conn, news_id: int) -> Optional[Dict[str, Any]]:
    """根据 ID 从 ship_news 读取最新记录（AI 重新处理后使用）。"""
    query = """
        SELECT
            id, title, link, date, summary, source_platform,
            tags, data, news_report, confidence_score
        FROM ship_news
        WHERE id = %s
    """
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(query, (news_id,))
        row = cursor.fetchone()
        return row
    finally:
        cursor.close()


async def sync_market_transactions(
    category: str = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    full_rebuild: bool = False,
    ai_concurrency: int = 3,
) -> Dict[str, Any]:
    """
    同步市场分析交易数据到 market_analysis_transactions 表。

    Args:
        category: "all" | "newbuilding" | "sale" | "charter"
        start_date/end_date: 按新闻日期筛选
        full_rebuild: True 时删除并重建所选范围数据；False 时仅处理尚未写入表的新闻
        ai_concurrency: AI 处理最大并发数
    """
    settings = get_settings()
    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    try:
        _ensure_market_analysis_table(conn)

        if category == "all":
            categories = list(_CATEGORY_MAP.values())
        else:
            cat_cn = _CATEGORY_MAP.get(category, category)
            categories = [cat_cn]

        cat_keys = {v: k for k, v in _CATEGORY_MAP.items()}
        result = {
            "processed_news": 0,
            "ai_processed_news": 0,
            "extracted_transactions": 0,
            "inserted_transactions": 0,
            "errors": [],
        }
        semaphore = asyncio.Semaphore(ai_concurrency)

        for cat in categories:
            if full_rebuild:
                _delete_existing_rows(conn, cat, start_date, end_date)

            rows = _fetch_candidate_news(conn, cat, start_date, end_date, full_rebuild)

            for row in rows:
                news_id = row.get("id")
                try:
                    records = _extract_records_from_news_item(row, cat)

                    # 没有提取到记录且（数据为空 或 全量重建）时，调用 AI 重新处理
                    if not records and (full_rebuild or _is_data_empty(row)):
                        # 全量重建时，若现有 data 里连 price/money 字段都没有，
                        # 说明原文大概率不含可量化交易信息，跳过 AI 处理避免浪费。
                        if full_rebuild and not _is_data_empty(row):
                            data_obj = _parse_json_loose(row.get("data"))
                            if not _has_price_in_data(data_obj, cat):
                                result["processed_news"] += 1
                                continue

                        await _ai_process_news(news_id, semaphore)
                        # AI 流程写入数据库，需重新读取最新记录再提取交易
                        refreshed = _fetch_news_by_id(conn, news_id)
                        if refreshed:
                            row = refreshed
                        records = _extract_records_from_news_item(row, cat)
                        result["ai_processed_news"] += 1

                    if records:
                        inserted = _insert_records(
                            conn, records, row, cat, cat_keys.get(cat, "")
                        )
                        result["inserted_transactions"] += inserted
                        result["extracted_transactions"] += len(records)

                    result["processed_news"] += 1
                except Exception as e:
                    result["errors"].append({"news_id": news_id, "error": str(e)})

        return result
    finally:
        conn.close()

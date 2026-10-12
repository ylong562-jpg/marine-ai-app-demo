# -*- coding: utf-8 -*-
"""
船厂交易自动同步脚本

功能：
1. 从 ship_news 中提取「今天」和「昨天」的「船舶建造」「新造船订单」两类新闻
2. 解析 data 字段中的 transactions
3. 按 shipyard 字段匹配 shipyard_data 表中的船厂
4. 将交易写入 shipyard_data.shipbuilding_transactions / newbuilding_transactions
5. 写入时按 (date, event_type/trade_type, ship_type, shipowner, specs) 去重
6. 保留历史数据，按 date 排序

使用方式：
    cd <repo>/backend
    source .venv/bin/activate
    python app/scripts/sync_shipyard_transactions.py [--dry-run] [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD]
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 确保能导入 app 配置
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings

settings = get_settings()

NEWS_TABLE = settings.NEWS_TABLE
SHIPYARD_TABLE = "shipyard_data"
VOCAB_TABLE = "normalized_entity_vocabulary"

SHIPBUILDING_TRANSACTIONS_COL = "shipbuilding_transactions"
NEWBUILDING_TRANSACTIONS_COL = "newbuilding_transactions"

TARGET_CATEGORIES = {"船舶建造", "新造船订单"}

# event_type 规范化映射（与 ai_news_core_content_extraction.py 保持一致）
_EVENT_TYPE_MAP = {
    "开工": "钢材切割",
    "钢材切割": "钢材切割",
    "铺龙骨": "铺设龙骨",
    "铺设龙骨": "铺设龙骨",
    "上船台": "铺设龙骨",
    "下水": "下水",
    "出坞": "下水",
    "命名": "下水",
    "吉水": "下水",
    "顺利入水": "下水",
    "试航": "试航",
    "海试": "试航",
    "调试": "试航",
    "交付": "交付",
    "交船": "交付",
    "交接船": "交付",
    "启航": "交付",
}


def _get_connection():
    import mysql.connector

    return mysql.connector.connect(**settings.NEWS_DB_CONFIG)


def _ensure_columns():
    """确保 shipyard_data 表存在两个新字段。"""
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"DESCRIBE {SHIPYARD_TABLE}")
        existing_cols = {row[0] for row in cursor.fetchall()}
        for col in (SHIPBUILDING_TRANSACTIONS_COL, NEWBUILDING_TRANSACTIONS_COL):
            if col not in existing_cols:
                cursor.execute(
                    f"ALTER TABLE {SHIPYARD_TABLE} ADD COLUMN {col} TEXT"
                )
                print(f"[DB] 新增字段 {col}")
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def _load_shipyard_vocab() -> Dict[str, str]:
    """加载船厂别名词表：alias_name -> canonical_name。

    数据来自统一的 normalized_entity_vocabulary（entity_type='shipyard'），
    将 canonical_name 与 aliases 全部展开为 alias -> canonical 映射。
    """
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            SELECT canonical_name, aliases
            FROM {VOCAB_TABLE}
            WHERE entity_type = 'shipyard'
              AND snapshot_date = CURDATE()
            """
        )
        vocab: Dict[str, str] = {}
        for canonical, aliases_json in cursor.fetchall():
            canonical = str(canonical).strip()
            if not canonical:
                continue
            vocab[canonical] = canonical
            aliases = []
            if aliases_json:
                try:
                    parsed = json.loads(aliases_json)
                    if isinstance(parsed, list):
                        aliases = [str(a).strip() for a in parsed if str(a).strip()]
                except Exception:
                    aliases = [str(aliases_json).strip()]
            for alias in aliases:
                if alias and alias != canonical:
                    vocab[alias] = canonical
        return vocab
    finally:
        cursor.close()
        conn.close()


def _load_shipyards() -> List[Dict[str, Any]]:
    """加载所有船厂名称和 ID。"""
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT id, Local_name, English_name FROM {SHIPYARD_TABLE}"
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def _build_shipyard_index(shipyards: List[Dict[str, Any]], vocab: Dict[str, str]):
    """
    构建船厂匹配索引。
    返回函数：shipyard_name -> shipyard_id 或 None
    """
    # 精确匹配：Local_name / English_name
    exact_local = {}
    exact_english = {}
    for sy in shipyards:
        sy_id = sy["id"]
        local = str(sy.get("Local_name") or "").strip()
        english = str(sy.get("English_name") or "").strip()
        if local:
            exact_local[local] = sy_id
        if english:
            exact_english[english] = sy_id

    # 通过词表标准化后的精确匹配
    canonical_local = {}
    for sy in shipyards:
        sy_id = sy["id"]
        local = str(sy.get("Local_name") or "").strip()
        if local:
            canonical_local[local] = sy_id

    def match(name: str) -> Optional[int]:
        if not name:
            return None
        name = str(name).strip()
        if not name:
            return None

        # 1. 精确匹配 Local_name
        if name in exact_local:
            return exact_local[name]

        # 2. 精确匹配 English_name
        if name in exact_english:
            return exact_english[name]

        # 3. 词表标准化后匹配 Local_name
        canonical = vocab.get(name)
        if canonical and canonical in canonical_local:
            return canonical_local[canonical]

        # 4. 子串匹配（兜底，优先 Local_name）
        for sy in shipyards:
            local = str(sy.get("Local_name") or "").strip()
            english = str(sy.get("English_name") or "").strip()
            if local and (local in name or name in local):
                return sy["id"]
            if english and (english in name or name in english):
                return sy["id"]

        return None

    return match


def _parse_json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _normalize_event_type(event_type: Optional[str]) -> Optional[str]:
    """规范化 event_type 到五种标准值（钢材切割、铺设龙骨、下水、试航、交付）；无法识别或无值时返回 None。"""
    if not event_type:
        return None
    return _EVENT_TYPE_MAP.get(str(event_type).strip())


def _extract_transactions(data_text: str, category: str) -> List[Dict[str, Any]]:
    """
    从新闻 data 字段解析出交易列表。
    兼容格式：
    - 单条字典
    - 含 transactions 数组的字典
    - 主维度分组数组 [{"main_tag": "...", "data": "..."}]
    """
    if not data_text:
        return []

    data = _parse_json_safe(data_text)
    if data is None:
        return []

    results = []

    # 如果 data 是主维度分组数组，先找到对应 category 的分组
    if isinstance(data, list) and data and isinstance(data[0], dict) and "main_tag" in data[0]:
        for group in data:
            if group.get("main_tag") == category:
                group_data = group.get("data")
                if isinstance(group_data, str):
                    group_data = _parse_json_safe(group_data)
                results.extend(_flatten_transactions(group_data))
        return results

    # 否则直接解析
    return _flatten_transactions(data)


def _flatten_transactions(data: Any) -> List[Dict[str, Any]]:
    """将 data 对象/数组统一展开为 transaction 字典列表。"""
    if not data:
        return []
    if isinstance(data, dict):
        # 如果包含 transactions 数组，展开它
        txs = data.get("transactions")
        if isinstance(txs, list):
            return [tx for tx in txs if isinstance(tx, dict)]
        return [data]
    if isinstance(data, list):
        out = []
        for item in data:
            if isinstance(item, dict):
                txs = item.get("transactions")
                if isinstance(txs, list):
                    out.extend([tx for tx in txs if isinstance(tx, dict)])
                else:
                    out.append(item)
        return out
    return []


def _transaction_key(tx: Dict[str, Any], category: str) -> str:
    """生成交易去重 key。"""
    if category == "船舶建造":
        type_key = _normalize_event_type(tx.get("event_type")) or ""
    else:
        type_key = str(tx.get("trade_type") or "")

    parts = [
        str(tx.get("date") or ""),
        type_key,
        str(tx.get("ship_type") or ""),
        str(tx.get("shipowner") or ""),
        str(tx.get("specs") or ""),
    ]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def _format_date(value: Any) -> Optional[str]:
    """把 MySQL date/datetime 或字符串统一为 YYYY-MM-DD 字符串。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10] if value else None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _prepare_transaction(tx: Dict[str, Any], category: str, news: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """清洗单条 transaction，添加来源新闻 ID 和链接。"""
    if not isinstance(tx, dict):
        return None

    shipyard = tx.get("shipyard")
    if not shipyard:
        return None

    cleaned = {
        "news_id": news.get("id"),
        "news_link": news.get("link"),
        "news_date": _format_date(news.get("date")),
    }

    if category == "船舶建造":
        cleaned.update({
            "date": _format_date(tx.get("date")) or _format_date(news.get("date")),
            "event_type": _normalize_event_type(tx.get("event_type")),
            "shipowner": tx.get("shipowner"),
            "shipyard": shipyard,
            "ship_type": tx.get("ship_type"),
            "specs": tx.get("specs"),
            "vessel_name": tx.get("vessel_name"),
            "quantity": tx.get("quantity"),
        })
    else:  # 新造船订单
        cleaned.update({
            "date": _format_date(tx.get("date")) or _format_date(news.get("date")),
            "trade_type": tx.get("trade_type"),
            "shipowner": tx.get("shipowner"),
            "shipyard": shipyard,
            "ship_type": tx.get("ship_type"),
            "quantity": tx.get("quantity"),
            "specs": tx.get("specs"),
            "price": tx.get("price"),
            "delivery": tx.get("delivery"),
            "power": tx.get("power"),
        })

    # 移除所有 None / 空字符串值，保留有意义的字段
    return {k: v for k, v in cleaned.items() if v not in (None, "", "null", "None")}


def _merge_transactions(existing_text: Optional[str], new_txs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并现有 transactions 和新的 transactions，去重并按 date 倒序。"""
    existing = []
    if existing_text:
        parsed = _parse_json_safe(existing_text)
        if isinstance(parsed, list):
            existing = parsed

    seen = set()
    merged = []
    for tx in existing + new_txs:
        if not isinstance(tx, dict):
            continue
        category = "船舶建造" if "event_type" in tx else "新造船订单"
        key = _transaction_key(tx, category)
        if key in seen:
            continue
        seen.add(key)
        merged.append(tx)

    # 按 date 倒序，无 date 的放最后
    def _sort_key(tx):
        d = str(tx.get("date") or "")
        return (d == "", d)

    merged.sort(key=_sort_key, reverse=False)  # 空 date 排最后
    merged.sort(key=lambda x: str(x.get("date") or ""), reverse=True)  # 有 date 倒序
    return merged


def sync_shipyard_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    执行同步任务。
    默认处理今天和昨天的新闻。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = start_date or yesterday
    end_date = end_date or today

    print(f"[Sync] 处理日期范围: {start_date} ~ {end_date}")

    _ensure_columns()

    vocab = _load_shipyard_vocab()
    shipyards = _load_shipyards()
    match_shipyard = _build_shipyard_index(shipyards, vocab)

    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 查询目标日期范围内、主维度包含目标类别的新闻
        query = f"""
            SELECT id, title, link, date, tags, data
            FROM {NEWS_TABLE}
            WHERE date >= %s AND date <= %s
              AND (
                  JSON_CONTAINS(NULLIF(tags, ''), %s, '$.main_tags')
                  OR JSON_CONTAINS(NULLIF(tags, ''), %s, '$.main_tags')
              )
            ORDER BY date DESC, id DESC
        """
        cursor.execute(query, (start_date, end_date, json.dumps("船舶建造"), json.dumps("新造船订单")))
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    print(f"[Sync] 查询到 {len(rows)} 条候选新闻")

    # 按船厂 ID + 类别聚合 transactions
    pending: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    skipped = []

    for news in rows:
        tags = _parse_json_safe(news.get("tags"))
        main_tags = tags.get("main_tags", []) if isinstance(tags, dict) else []
        for category in TARGET_CATEGORIES:
            if category not in main_tags:
                continue
            transactions = _extract_transactions(news.get("data"), category)
            for tx in transactions:
                shipyard_id = match_shipyard(tx.get("shipyard"))
                if not shipyard_id:
                    skipped.append({
                        "news_id": news.get("id"),
                        "category": category,
                        "shipyard": tx.get("shipyard"),
                        "reason": "未匹配到船厂",
                    })
                    continue

                prepared = _prepare_transaction(tx, category, news)
                if not prepared:
                    continue

                key = (shipyard_id, category)
                pending.setdefault(key, []).append(prepared)

    print(f"[Sync] 解析出 {sum(len(v) for v in pending.values())} 条待写入交易，{len(skipped)} 条无法匹配船厂")

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "date_range": [start_date, end_date],
            "updated_shipyards": len(pending),
            "pending_transactions": sum(len(v) for v in pending.values()),
            "pending_detail": {f"{k[0]}:{k[1]}": v for k, v in pending.items()},
            "skipped": skipped,
        }

    # 写入数据库
    conn = _get_connection()
    cursor = conn.cursor()
    updated = 0
    try:
        for (shipyard_id, category), new_txs in pending.items():
            col = SHIPBUILDING_TRANSACTIONS_COL if category == "船舶建造" else NEWBUILDING_TRANSACTIONS_COL

            cursor.execute(f"SELECT {col} FROM {SHIPYARD_TABLE} WHERE id = %s", (shipyard_id,))
            row = cursor.fetchone()
            existing_text = row[0] if row else None

            merged = _merge_transactions(existing_text, new_txs)
            merged_text = json.dumps(merged, ensure_ascii=False)

            cursor.execute(
                f"UPDATE {SHIPYARD_TABLE} SET {col} = %s WHERE id = %s",
                (merged_text, shipyard_id),
            )
            # 只要成功执行 UPDATE 即计数（即使值相同 rowcount 也可能为 0）
            updated += 1

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    print(f"[Sync] 成功更新 {updated} 条船厂记录")

    return {
        "success": True,
        "dry_run": False,
        "date_range": [start_date, end_date],
        "updated_shipyards": updated,
        "pending_transactions": sum(len(v) for v in pending.values()),
        "skipped": skipped,
    }


def main():
    parser = argparse.ArgumentParser(description="同步船厂交易信息")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不写入数据库")
    parser.add_argument("--start-date", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", help="结束日期 YYYY-MM-DD")
    args = parser.parse_args()

    result = sync_shipyard_transactions(
        start_date=args.start_date,
        end_date=args.end_date,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

"""
Shipyard import service.

Encapsulates the core import logic originally in app.api.shipyards so it can be
reused by /shipyards/import and /reprocess/import without depending on FastAPI
request context.
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.shipyard_ai.deduplicator import (
    _upsert_shipyard_entity,
    find_matching_entity,
    load_shipyard_entities,
)
from app.shipyard_ai.field_specs import get_field_spec, _normalize_group
from app.shipyard_ai.name_matcher import ai_match_shipyard_name

logger = logging.getLogger(__name__)

# Columns excluded from editable import (id cannot be modified; create_info is managed server-side)
IMPORT_EXCLUDED_COLS = {
    "id",
    "introduce",
    "url",
    "crawled_at",
    "ai_processed",
    "processed_at",
    "Shipyard_ID",
    "create_info",
    "update_time",
    "update_person",
}


def _sanitize_value_for_db(field: str, value: Any) -> Any:
    """把导入值转换为 MySQL 可接受的格式。"""
    from datetime import date, datetime

    if value is None:
        return None

    # 统一空字符串为 NULL
    if isinstance(value, str) and value.strip() == "":
        return None

    # 日期/时间对象转字符串
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")

    # JSON 字段统一序列化
    spec = get_field_spec(field)
    is_json = spec.is_json if spec else False
    if is_json and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)

    # utilization_rate 等 decimal 字段做数值转换
    if field == "utilization_rate":
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    return value


def _sanitize_row_for_db(row: dict) -> dict:
    return {k: _sanitize_value_for_db(k, v) for k, v in row.items()}


def _normalize_value_for_compare(v):
    """Normalize empty values to None for accurate change detection."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    if isinstance(v, (list, dict)) and len(v) == 0:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v).strip()


def _compute_field_changes(row: dict, existing_row: dict | None) -> list[dict]:
    """Compare submitted data with existing DB data and generate field-level change records."""
    is_new = existing_row is None
    changes = []
    for k, v in row.items():
        if k in IMPORT_EXCLUDED_COLS or k == "id":
            continue
        new_norm = _normalize_value_for_compare(v)
        if is_new:
            if new_norm is None:
                continue
            changes.append({"field": k, "old": None, "new": v})
        else:
            old = existing_row.get(k)
            old_norm = _normalize_value_for_compare(old)
            if new_norm != old_norm:
                changes.append({"field": k, "old": old, "new": v})
    return changes


def _build_create_info(manager_username: str, existing_info: str | bytes | list | dict | None = None) -> str:
    """Build create_info JSON array with a new {update_person, update_time} entry."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "update_person": manager_username,
        "update_time": now,
    }
    history: list[dict] = []
    if existing_info:
        try:
            if isinstance(existing_info, (bytes, bytearray)):
                parsed = json.loads(existing_info.decode("utf-8", errors="ignore"))
            elif isinstance(existing_info, str):
                parsed = json.loads(existing_info)
            else:
                parsed = existing_info
            if isinstance(parsed, list):
                history = parsed
        except Exception:
            pass
    history.append(entry)
    return json.dumps(history, ensure_ascii=False)


def _record_shipyard_history(cursor, shipyard_id: int, update_person: str, update_time: str, changes: list[dict]):
    """Write field-level change records to shipyard_update_history table."""
    if not changes:
        return
    cursor.execute(
        "INSERT INTO shipyard_update_history (shipyard_id, update_time, update_person, changes) VALUES (%s, %s, %s, %s)",
        (shipyard_id, update_time, update_person, json.dumps(changes, ensure_ascii=False)),
    )


def import_shipyard_rows(
    rows: List[Dict[str, Any]],
    manager_username: str,
    db_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Batch upsert shipyard rows into shipyard_data.

    Args:
        rows: List of shipyard dicts. Each row may contain an "id" to indicate update;
              id <= 0 or missing means insert.
        manager_username: Username of the operator (used for create_info and history).
        db_config: Optional MySQL config dict. Defaults to settings.NEWS_DB_CONFIG.

    Returns:
        {"success": True, "updated": int, "inserted": int, "errors": list[str],
         "id_mapping": dict, "message": str}
    """
    import mysql.connector

    settings = get_settings()
    config = db_config or settings.NEWS_DB_CONFIG
    conn = mysql.connector.connect(**config)
    cursor = conn.cursor()

    try:
        updated = 0
        inserted = 0
        errors: List[str] = []
        id_mapping: Dict[int | str, int] = {}
        new_shipyard_names: List[tuple[Optional[str], Optional[str], Optional[str]]] = []

        for raw_row in rows:
            row = _sanitize_row_for_db(raw_row)
            if "group" in row:
                row["group"] = _normalize_group(row["group"])
            sid = row.get("id")
            is_new = sid is None or (isinstance(sid, (int, float)) and sid <= 0)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if is_new:
                create_info_json = _build_create_info(manager_username)
                existing_row = None
            else:
                cursor.execute("SELECT * FROM shipyard_data WHERE id = %s", (sid,))
                existing = cursor.fetchone()
                if existing:
                    cols = [desc[0] for desc in cursor.description]
                    existing_row = dict(zip(cols, existing))
                else:
                    existing_row = None
                create_info_json = _build_create_info(
                    manager_username,
                    existing_row.get("create_info") if existing_row else None,
                )

            field_changes = _compute_field_changes(row, existing_row)

            if is_new:
                fields = ["`create_info`", "`update_time`", "`update_person`"]
                values = [create_info_json, now, manager_username]
            else:
                fields = ["`create_info` = %s", "`update_time` = %s", "`update_person` = %s"]
                values = [create_info_json, now, manager_username]

            for k, v in row.items():
                if k in IMPORT_EXCLUDED_COLS:
                    continue
                if is_new and k == "id":
                    continue
                if v is None:
                    if is_new:
                        fields.append(f"`{k}`")
                        values.append(None)
                    else:
                        fields.append(f"`{k}` = NULL")
                else:
                    if is_new:
                        fields.append(f"`{k}`")
                    else:
                        fields.append(f"`{k}` = %s")
                    if isinstance(v, (dict, list)):
                        values.append(json.dumps(v, ensure_ascii=False))
                    else:
                        values.append(v)

            if is_new:
                sql = f"INSERT INTO shipyard_data ({', '.join(fields)}) VALUES ({', '.join(['%s'] * len(values))})"
            else:
                values.append(sid)
                sql = f"UPDATE shipyard_data SET {', '.join(fields)} WHERE id = %s"

            try:
                cursor.execute(sql, values)
                if is_new:
                    inserted += cursor.rowcount
                    new_id = cursor.lastrowid
                    if new_id and sid is not None:
                        id_mapping[sid] = new_id
                    new_shipyard_names.append(
                        (row.get("Local_name"), row.get("English_name"), row.get("Abreviation"))
                    )
                    if field_changes:
                        _record_shipyard_history(cursor, new_id, manager_username, now, field_changes)
                else:
                    updated += cursor.rowcount
                    if field_changes:
                        _record_shipyard_history(cursor, sid, manager_username, now, field_changes)
            except Exception as e:
                errors.append(f"{'新增' if is_new else f'id={sid} 更新'}失败: {str(e)}")

        conn.commit()

        # Sync newly imported shipyards into the entity registry.
        # 先批量加载已有船厂实体，并对新名称做 AI/规则匹配，避免同一主体的不同写法被重复注册。
        existing_entities = load_shipyard_entities()
        for local_name, english_name, abbreviation in new_shipyard_names:
            if not local_name or not str(local_name).strip():
                continue
            local_name = str(local_name).strip()

            matched = ai_match_shipyard_name(local_name, english_name, existing_entities)
            if not matched:
                matched = find_matching_entity(local_name, existing_entities)

            target_name = matched if matched else local_name
            row_id = _upsert_shipyard_entity(target_name, english_name, abbreviation)
            if row_id and matched and target_name != local_name:
                logger.info(
                    "船厂导入去重: %s -> %s", local_name, target_name
                )

        message_parts = []
        if inserted:
            message_parts.append(f"成功新增 {inserted} 条记录")
        if updated:
            message_parts.append(f"成功更新 {updated} 条记录")
        if errors:
            message_parts.append(f"{len(errors)} 条出错")

        return {
            "success": True,
            "updated": updated,
            "inserted": inserted,
            "errors": errors,
            "id_mapping": id_mapping,
            "message": "；".join(message_parts) or "导入完成",
        }
    except Exception as e:
        conn.rollback()
        logger.exception("Shipyard import failed")
        return {
            "success": False,
            "updated": 0,
            "inserted": 0,
            "errors": [str(e)],
            "id_mapping": {},
            "message": f"导入失败: {e}",
        }
    finally:
        cursor.close()
        conn.close()

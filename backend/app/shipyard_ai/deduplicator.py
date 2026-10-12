"""
Shipyard AI deduplication against the entity registry and MySQL shipyard_data.

This module is intentionally decoupled from FastAPI/SQLAlchemy so it can be
called directly from the extraction pipeline, scripts, or tests.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.news_engine.detectors.entity_deduplication import EntityRegistry
from app.news_engine.entity_normalizer import normalize_shipyard_name
from app.shipyard_ai.ai_extract_shipyard_info import call_qwen_api, generate_abbreviation
from app.shipyard_ai.name_matcher import ai_match_shipyard_name

logger = logging.getLogger(__name__)

# MySQL connector is the same driver used elsewhere in the project.
try:
    import mysql.connector

    MYSQL_AVAILABLE = True
except ImportError:  # pragma: no cover
    MYSQL_AVAILABLE = False

# Shared entity registry instance used for all shipyard lookups/updates.
_registry = EntityRegistry()


def _candidate_name(record: dict) -> str:
    """Return the best available shipyard name from the extracted record."""
    for key in ("Local_name", "English_name"):
        value = record.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def _clean_alias(value: Any) -> Optional[str]:
    """Return a stripped string or None for non-string/empty values."""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _is_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _looks_english(text: str) -> bool:
    """True if text contains Latin letters and no Chinese characters."""
    if _is_chinese(text):
        return False
    return any("\u0041" <= ch <= "\u007A" or "\u00C0" <= ch <= "\u024F" for ch in text)


def _enrich_create_info(record: Dict[str, Any]) -> None:
    """Parse create_info JSON and add update_person / update_time.

    Falls back to legacy ``account_name`` / ``modify_time`` keys.
    """
    create_info = record.get("create_info")
    if not create_info:
        return

    parsed = create_info
    if isinstance(create_info, (str, bytes)):
        try:
            parsed = json.loads(create_info.decode("utf-8", errors="ignore") if isinstance(create_info, bytes) else create_info)
        except Exception:
            return

    if not isinstance(parsed, list) or not parsed:
        return

    latest = parsed[-1]
    if not isinstance(latest, dict):
        return

    update_person = latest.get("update_person") or latest.get("account_name")
    update_time = latest.get("update_time") or latest.get("modify_time")

    if update_person:
        record["update_person"] = update_person
    if update_time:
        record["update_time"] = update_time


def load_shipyard_entities() -> List[Dict[str, Any]]:
    """Load all shipyard canonical names and aliases from the entity registry."""
    try:
        return _registry._load_entities_by_type("shipyard")
    except Exception as exc:
        logger.warning("Failed to load shipyard entities: %s", exc)
        return []


def find_matching_entity(name: str, entities: List[Dict[str, Any]]) -> Optional[str]:
    """
    Match ``name`` against canonical names and aliases using direct string rules.

    Priority:
      1. Exact case-insensitive canonical name match.
      2. Exact case-insensitive alias match.
      3. Substring case-insensitive alias match.

    Returns the matched canonical name or ``None``.
    """
    if not name:
        return None

    normalized = name.strip().lower()

    # 1. Exact canonical match.
    for entity in entities:
        if entity["canonical_name"].strip().lower() == normalized:
            return entity["canonical_name"]

    # 2. Exact alias match.
    for entity in entities:
        for alias in entity["aliases"]:
            if alias.strip().lower() == normalized:
                return entity["canonical_name"]

    # 3. Substring alias match (alias contained in name or vice versa).
    for entity in entities:
        for alias in entity["aliases"]:
            alias_lower = alias.strip().lower()
            if alias_lower and (normalized in alias_lower or alias_lower in normalized):
                return entity["canonical_name"]

    return None


def _extract_names_from_entity(entity: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract (local_name, english_name, abbreviation) from a registry entity.

    - ``local_name`` is the entity ``canonical_name``.
    - ``english_name`` is the dedicated ``english_name`` field if present and
      English-looking; otherwise the best English-form alias (explicit ``en:``
      prefix wins, otherwise the first non-abbreviation English-looking alias).
    - ``abbreviation`` is a short (<=8) uppercase/mixed alias without Chinese
      characters (explicit ``abbr:`` prefix wins).
    """
    local_name = entity.get("canonical_name")
    stored_english = str(entity.get("english_name") or "").strip()
    aliases = entity.get("aliases") or []
    if stored_english and _looks_english(stored_english):
        # 专用英文字段可信，直接使用；缩写仍从 aliases 推导
        abbreviation = None
        explicit_abbr: Optional[str] = None
        for alias in aliases:
            if not isinstance(alias, str):
                continue
            alias = alias.strip()
            if not alias:
                continue
            if alias.lower().startswith("abbr:"):
                explicit_abbr = alias[5:].strip()
                break
        abbreviation = explicit_abbr
        if abbreviation is None:
            for alias in aliases:
                alias = alias.strip()
                if len(alias) <= 8 and not _is_chinese(alias):
                    if alias.isupper() and " " not in alias:
                        abbreviation = alias
                        break
        if abbreviation is None and len(aliases) >= 2:
            second = str(aliases[1]).strip()
            if len(second) <= 8 and not _is_chinese(second):
                abbreviation = second
        return local_name, stored_english, abbreviation

    explicit_en: Optional[str] = None
    explicit_abbr: Optional[str] = None
    parsed_aliases: list[str] = []

    for alias in aliases:
        if not isinstance(alias, str):
            continue
        alias = alias.strip()
        if not alias:
            continue
        lower = alias.lower()
        if lower.startswith("en:"):
            explicit_en = alias[3:].strip()
            parsed_aliases.append(explicit_en)
        elif lower.startswith("abbr:"):
            explicit_abbr = alias[5:].strip()
            parsed_aliases.append(explicit_abbr)
        else:
            parsed_aliases.append(alias)

    # Determine abbreviation.
    abbreviation = explicit_abbr
    if abbreviation is None:
        for alias in parsed_aliases:
            if len(alias) <= 8 and not _is_chinese(alias):
                # Prefer compact acronyms without spaces.
                if alias.isupper() and " " not in alias:
                    abbreviation = alias
                    break
        if abbreviation is None and len(parsed_aliases) >= 2:
            second = parsed_aliases[1]
            if len(second) <= 8 and not _is_chinese(second):
                abbreviation = second

    # Determine English name.
    english_name = explicit_en
    if english_name is None:
        for alias in parsed_aliases:
            if alias == abbreviation:
                continue
            if _looks_english(alias):
                english_name = alias
                break
        if english_name is None and parsed_aliases:
            first = parsed_aliases[0]
            if first != abbreviation and _looks_english(first):
                english_name = first

    return local_name, english_name, abbreviation


def _looks_english(text: str) -> bool:
    if not text or not text.strip():
        return False
    return not bool(re.search(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", text))


def _ensure_english_name(canonical: str, english: Optional[str]) -> Optional[str]:
    """确保 shipyard 的 canonical_name 有对应的英文名称；缺失时调用 AI 生成。"""
    if english and _looks_english(english):
        return english.strip()
    if _looks_english(canonical):
        return canonical.strip()
    try:
        from app.news_engine.entity_normalizer import ShipyardNameNormalizer
        normalizer = ShipyardNameNormalizer()
        return normalizer._ensure_english_name(canonical)
    except Exception as exc:
        logger.warning("Failed to ensure english name for %s: %s", canonical, exc)
        return None


def _upsert_shipyard_entity(
    canonical_name: str,
    english_name: Optional[str],
    abbreviation: Optional[str],
) -> Optional[int]:
    """
    [已禁用] 词表为只读（/path/to/normalized_shipyard_vocabulary.json），
    不再将新船厂实体写入实体词表，只允许检测匹配，不允许录入。
    保留函数签名以兼容现有调用方，始终返回 None。
    """
    logger.info(
        "词表为只读，跳过船厂实体录入: %s (english=%s, abbr=%s)",
        canonical_name, english_name, abbreviation,
    )
    return None


def add_new_shipyard_entity(record: dict) -> Optional[int]:
    """
    [已禁用] 词表为只读，不再从提取记录注册/更新船厂实体。
    保留函数签名以兼容现有调用方，始终返回 None。
    """
    local_name = _clean_alias(record.get("Local_name"))
    english_name = _clean_alias(record.get("English_name"))
    abbreviation = _clean_alias(record.get("Abreviation"))
    canonical = local_name or english_name
    if not canonical:
        return None
    return _upsert_shipyard_entity(canonical, english_name, abbreviation)


# Fields that should never participate in AI field comparison.
_MATCH_EXCLUDED_FIELDS = {
    "id",
    "create_info",
    "introduce",
    "url",
    "crawled_at",
    "ai_processed",
    "processed_at",
    "Shipyard_ID",
    "canonical_name",
    "shipyard_name",
    "file_name",
    "file_type",
    "error",
    "production_lines",
    "main_ship_types_now",
    "ship_types_delivered",
    "update_time",
    "update_person",
    "is_verified",
}


def _prepare_match_payload(new: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact payload containing only comparable scalar fields."""
    payload: Dict[str, Any] = {}
    for key, old_value in old.items():
        if key in _MATCH_EXCLUDED_FIELDS:
            continue
        if old_value is None or old_value == "":
            continue
        if key not in new:
            continue
        new_value = new[key]
        # Stringify complex values for comparison.
        payload[key] = {
            "new": new_value if not isinstance(new_value, (dict, list)) else json.dumps(new_value, ensure_ascii=False),
            "old": old_value if not isinstance(old_value, (dict, list)) else json.dumps(old_value, ensure_ascii=False),
        }
    return payload


def _smart_match_fields(new: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, bool]:
    """
    Use Qwen to decide, field by field, whether the new extraction and the
    existing DB record contain substantively the same value.

    Returns a dict mapping field name -> True/False. Missing fields default
    to False (treat as different). On API/parsing failure, returns an empty
    dict so the UI falls back to showing the toggle for every field.
    """
    payload = _prepare_match_payload(new, old)
    if not payload:
        return {}

    prompt = """You are comparing two shipyard records: "new" (newly extracted) and "old" (from the database).
For each field below, decide whether the "new" value and "old" value are substantively the same.
Rules:
- Return ONLY a JSON object mapping each field name to true or false.
- true  = the two values mean the same thing (ignore formatting differences, case differences, or minor wording).
- false = the values are different, or one is empty/null while the other has real data.
- If the old value is a more complete/superset version of the new value, treat as true.
- Do not include any explanation, markdown, or code block; return raw JSON only.

Fields to compare:
""" + json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        result_str = call_qwen_api(prompt, "")
        if not result_str:
            return {}
        result_str = result_str.strip()
        if result_str.startswith("```json"):
            result_str = result_str[7:]
        if result_str.startswith("```"):
            result_str = result_str[3:]
        if result_str.endswith("```"):
            result_str = result_str[:-3]
        result_str = result_str.strip()
        parsed = json.loads(result_str)
        if isinstance(parsed, dict):
            return {str(k): bool(v) for k, v in parsed.items()}
    except Exception as exc:
        logger.warning("Smart field matching failed: %s", exc)
    return {}


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Make a MySQL row JSON-safe (dates -> ISO strings, keep JSON columns)."""
    result: Dict[str, Any] = {}
    for key, value in row.items():
        if value is None:
            result[key] = None
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


def find_db_match(name: str) -> Optional[Dict[str, Any]]:
    """
    Query MySQL ``Time_Series.shipyard_data`` for a matching ``Local_name``.

    Matches case-insensitive exact name or substring. Returns the first hit or
    ``None`` if no match is found or the database is unreachable.
    """
    if not name or not MYSQL_AVAILABLE:
        return None

    result = batch_find_db_matches([name])
    return result.get(name)


def batch_find_db_matches(names: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    Batch query MySQL ``Time_Series.shipyard_data`` for matching ``Local_name`` values.

    Strategy:
      1. Case-insensitive exact match for all names in a single query.
      2. Substring (LIKE) match for names without an exact hit, also in a single query.

    Returns a dict mapping each input name to its best matching row or ``None``.
    Uses only one DB connection for the whole batch.
    """
    cleaned = []
    seen = set()
    for n in names:
        if not n:
            continue
        text = str(n).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        cleaned.append(text)

    if not cleaned or not MYSQL_AVAILABLE:
        return {n: None for n in names}

    settings = get_settings()
    config = settings.NEWS_DB_CONFIG
    matches: Dict[str, Optional[Dict[str, Any]]] = {n: None for n in names}

    try:
        cnx = mysql.connector.connect(**config)
        cursor = cnx.cursor(dictionary=True)

        # 1. Exact case-insensitive matches.
        placeholders = ", ".join(["%s"] * len(cleaned))
        cursor.execute(
            f"""
            SELECT * FROM shipyard_data
            WHERE LOWER(Local_name) IN ({placeholders})
            """,
            tuple(c.lower() for c in cleaned),
        )
        exact_rows = cursor.fetchall()
        matched_lower = set()
        for row in exact_rows:
            serialized = _serialize_row(row)
            local = str(row.get("Local_name") or "").strip().lower()
            for name in cleaned:
                name_lower = name.lower()
                if name_lower == local and name_lower not in matched_lower:
                    matches[name] = serialized
                    matched_lower.add(name_lower)
                    break

        # 2. Substring matches for remaining names.
        remaining = [c for c in cleaned if c.lower() not in matched_lower]
        if remaining:
            values_sql = " UNION ALL ".join(["SELECT %s AS name"] * len(remaining))
            params = tuple(r.lower() for r in remaining)
            cursor.execute(
                f"""
                SELECT s.*, n.name AS query_name
                FROM shipyard_data s
                JOIN (
                    {values_sql}
                ) n
                ON LOWER(s.Local_name) LIKE CONCAT('%%', n.name, '%%')
                """,
                params,
            )
            like_rows = cursor.fetchall()
            # Prefer the longest Local_name for each query name (most specific match).
            for row in like_rows:
                query_name = str(row.get("query_name") or "").strip().lower()
                if query_name in matched_lower:
                    continue
                serialized = _serialize_row(row)
                local_len = len(str(row.get("Local_name") or "").strip())
                current = matches.get(next((n for n in cleaned if n.lower() == query_name), ""))
                if current is None or local_len > len(str(current.get("Local_name") or "").strip()):
                    for name in cleaned:
                        if name.lower() == query_name:
                            matches[name] = serialized
                            break

        cursor.close()
        cnx.close()
    except Exception as exc:  # pragma: no cover
        logger.warning("MySQL shipyard_data batch lookup failed: %s", exc)

    return matches


def _insert_placeholder_shipyard(
    record: Dict[str, Any],
    update_person: str = "AI",
) -> Optional[Dict[str, Any]]:
    """
    Insert a minimal placeholder row into MySQL ``shipyard_data``.

    Only ``Local_name``, ``English_name``, ``Abreviation`` are written;
    ``create_info`` is left null. Returns the inserted row or ``None``.
    Also records the creation in ``shipyard_update_history``.
    """
    if not MYSQL_AVAILABLE:
        return None

    local_name = _clean_alias(record.get("Local_name"))
    if not local_name:
        return None

    english_name = _clean_alias(record.get("English_name"))
    abbreviation = _clean_alias(record.get("Abreviation"))

    settings = get_settings()
    config = settings.NEWS_DB_CONFIG

    try:
        cnx = mysql.connector.connect(**config)
        cursor = cnx.cursor(dictionary=True)
        cursor.execute(
            """
            INSERT INTO shipyard_data (Local_name, English_name, Abreviation, create_info)
            VALUES (%s, %s, %s, %s)
            """,
            (local_name, english_name, abbreviation, None),
        )
        cnx.commit()
        new_id = cursor.lastrowid

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changes = []
        if local_name:
            changes.append({"field": "Local_name", "old": None, "new": local_name})
        if english_name:
            changes.append({"field": "English_name", "old": None, "new": english_name})
        if abbreviation:
            changes.append({"field": "Abreviation", "old": None, "new": abbreviation})
        changes.append({"field": "source", "old": None, "new": "AI extraction placeholder"})
        if changes:
            cursor.execute(
                """
                INSERT INTO shipyard_update_history
                    (shipyard_id, update_time, update_person, changes)
                VALUES (%s, %s, %s, %s)
                """,
                (new_id, now, update_person, json.dumps(changes, ensure_ascii=False)),
            )
            cnx.commit()

        cursor.execute("SELECT * FROM shipyard_data WHERE id = %s", (new_id,))
        row = cursor.fetchone()
        cursor.close()
        cnx.close()
        return _serialize_row(row) if row else None
    except Exception as exc:
        logger.warning("Failed to insert placeholder shipyard row: %s", exc)
        return None


def deduplicate_shipyard_record(record: dict, add_new: bool = True) -> dict:
    """
    Deduplicate a single AI-extracted shipyard record.

    Args:
        record: Raw extraction result from the AI pipeline.
        add_new: If ``True`` and no registry match is found, insert a new
            shipyard entity into the registry.

    Returns:
        A wrapper dict containing the original record, duplicate status,
        canonical name, replacement candidate, existing DB record, and flag
        indicating whether a new entity was created.
    """
    name = _candidate_name(record)

    # No usable name: return a neutral wrapper without touching the registry.
    if not name:
        return {
            "extracted_data": record,
            "duplicate": False,
            "canonical_name": None,
            "replacement": None,
            "existing_record": None,
            "is_new_entity": False,
        }

    entities = load_shipyard_entities()
    local_name = _clean_alias(record.get("Local_name"))
    english_name = _clean_alias(record.get("English_name"))

    # 0. Normalize through the news AI entity vocabulary first.
    if local_name:
        normalized_local = normalize_shipyard_name(local_name)
        if normalized_local and normalized_local != local_name:
            record["Local_name"] = normalized_local
            local_name = _clean_alias(normalized_local)
        # Enrich English name / abbreviation from the registry entity if available.
        registry_entity = find_matching_entity(local_name, entities)
        if registry_entity:
            for entity in entities:
                if entity.get("canonical_name") == registry_entity:
                    stored_en = _clean_alias(entity.get("english_name"))
                    if stored_en and stored_en != local_name:
                        record["English_name"] = stored_en
                        english_name = stored_en
                    break

    # 1. AI matching first, then fall back to direct string rules.
    canonical_name = ai_match_shipyard_name(local_name, english_name, entities)
    matched_entity: Optional[Dict[str, Any]] = None
    if canonical_name:
        for entity in entities:
            if entity.get("canonical_name") == canonical_name:
                matched_entity = entity
                break

    if not canonical_name:
        canonical_name = find_matching_entity(name, entities)
        if canonical_name:
            for entity in entities:
                if entity.get("canonical_name") == canonical_name:
                    matched_entity = entity
                    break

    existing_record: Optional[Dict[str, Any]] = None
    replacement: Optional[Dict[str, Any]] = None
    is_new_entity = False
    same_fields: Dict[str, bool] = {}

    if canonical_name and matched_entity:
        duplicate = True
        extracted_local, extracted_english, extracted_abbr = _extract_names_from_entity(matched_entity)
        if extracted_local:
            record["Local_name"] = extracted_local
        if extracted_english:
            record["English_name"] = extracted_english
        if extracted_abbr:
            record["Abreviation"] = extracted_abbr

        existing_record = find_db_match(canonical_name)
        if existing_record is None:
            existing_record = find_db_match(name)

        if existing_record is None:
            existing_record = _insert_placeholder_shipyard(record)

        if existing_record:
            _enrich_create_info(existing_record)
            replacement = dict(record)
            for key, value in existing_record.items():
                if value is not None:
                    replacement[key] = value
            same_fields = _smart_match_fields(record, existing_record)
    else:
        duplicate = False
        canonical_name = None
        existing_record = find_db_match(name)
        if existing_record:
            _enrich_create_info(existing_record)
            replacement = dict(record)
            for key, value in existing_record.items():
                if value is not None:
                    replacement[key] = value
            same_fields = _smart_match_fields(record, existing_record)

        if add_new:
            abbr = _clean_alias(record.get("Abreviation"))
            if not abbr:
                eng = _clean_alias(record.get("English_name"))
                if eng:
                    generated = generate_abbreviation(eng, set())
                    if generated:
                        record["Abreviation"] = generated
            add_new_shipyard_entity(record)
            is_new_entity = True

    return {
        "extracted_data": record,
        "duplicate": duplicate,
        "canonical_name": canonical_name,
        "replacement": replacement,
        "existing_record": existing_record,
        "same_fields": same_fields,
        "is_new_entity": is_new_entity,
    }

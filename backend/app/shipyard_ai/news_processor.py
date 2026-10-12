"""
News reprocess pipeline for shipyard information extraction.

This module provides a side-effect-free flow used by the news reprocess task:
1. Determine whether a news article is shipyard-related.
2. Normalize extracted names against the shipyard entity registry.
3. Merge duplicate shipyards within the same batch using AI.
4. Visually deduplicate against the DB (no writes to DB or registry).
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.shipyard_ai.ai_extract_shipyard_info import call_qwen_api
from app.shipyard_ai.deduplicator import (
    _candidate_name,
    _clean_alias,
    _extract_names_from_entity,
    _serialize_row,
    batch_find_db_matches,
    find_db_match,
    find_matching_entity,
    load_shipyard_entities,
)
from app.shipyard_ai.name_matcher import ai_match_shipyard_name

logger = logging.getLogger(__name__)


# ========== 1. Shipyard news pre-detection ==========

SHIPYARD_KEYWORDS = {
    "shipyard", "shipyards", "shipbuilding", "ship builder", "shipbuilder",
    "dry dock", "drydock", "slipway", "building dock", "graving dock",
    "船厂", "造船厂", "船坞", "船台", "造船", "修船", "船舶重工",
    "重工", "造船基地", "海工基地",
}

SHIPYARD_ORDER_KEYWORDS = {
    "newbuild", "newbuilding", "new build", "new order", "ship order",
    "contract", "ordered", "to build", "will build", "delivery",
    "新造船订单", "新船订单", "造船订单", "订单", "交付",
}

SHIPYARD_REPAIR_KEYWORDS = {
    "repair", "repairs", "repairing", "conversion", "retrofit",
    "drydocking", "dry docking", "maintenance",
    "维修", "改装", "改造", "修理", "修船",
}


def is_shipyard_news(text: str, title: str = "", category: str = "") -> bool:
    """
    基于关键词快速判断文本是否与船厂相关。
    标题、摘要、分类中任一词命中即视为船厂新闻。
    """
    combined = f"{title} {text} {category}".lower()
    # 优先：船厂直接关键词
    for kw in SHIPYARD_KEYWORDS:
        if kw in combined:
            return True
    # 次优先：新造船/维修 + 船/造/建/坞/厂
    order_repair_kw = set(SHIPYARD_ORDER_KEYWORDS) | set(SHIPYARD_REPAIR_KEYWORDS)
    ship_kw = {"ship", "vessel", "yard", "dock", "build", "船", "舰", "艇", "造", "坞", "厂"}
    has_order_repair = any(kw in combined for kw in order_repair_kw)
    has_ship = any(kw in combined for kw in ship_kw)
    if has_order_repair and has_ship:
        return True
    return False


# ========== 2. Lexicon normalization ==========

def normalize_by_lexicon(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    使用 entity registry 规范化船厂名称。

    若命中词表，将 Local_name / English_name / Abreviation 替换为规范名称；
    未命中则保留原始值。
    """
    local_name = _clean_alias(record.get("Local_name"))
    english_name = _clean_alias(record.get("English_name"))

    entities = load_shipyard_entities()
    canonical_name = ai_match_shipyard_name(local_name, english_name, entities)
    matched_entity = None
    if canonical_name:
        for entity in entities:
            if entity.get("canonical_name") == canonical_name:
                matched_entity = entity
                break

    if not matched_entity:
        # Fallback to direct string matching.
        name = _candidate_name(record)
        canonical_name = find_matching_entity(name, entities)
        if canonical_name:
            for entity in entities:
                if entity.get("canonical_name") == canonical_name:
                    matched_entity = entity
                    break

    if matched_entity:
        extracted_local, extracted_english, extracted_abbr = _extract_names_from_entity(matched_entity)
        if extracted_local:
            record["Local_name"] = extracted_local
        if extracted_english:
            record["English_name"] = extracted_english
        if extracted_abbr:
            record["Abreviation"] = extracted_abbr
        record["canonical_name"] = canonical_name

    return record


# ========== 3. Intra-batch AI merge ==========

def _normalize_name_for_compare(name: Any) -> str:
    """标准化名称用于比较。"""
    if not name:
        return ""
    s = str(name).lower().strip()
    for suffix in [
        r"\s+co\.?\s*,?\s*ltd\.?\s*\.?$",
        r"\s+limited\s*$",
        r"\s+inc\.?\s*$",
        r"\s+corporation\s*$",
        r"\s+corp\.?\s*$",
        r"\s+company\s*$",
        r"\s+group\s*$",
        r"有限公司$",
        r"造船有限公司$",
        r"船厂$",
    ]:
        s = re.sub(suffix, "", s, flags=re.IGNORECASE)
    s = re.sub(r"[^\w\s]", "", s)
    return " ".join(s.split()).strip()


def _same_shipyard(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """判断两条提取结果是否指向同一船厂（名称精确/包含匹配）。"""
    names_a = [
        _normalize_name_for_compare(a.get("English_name")),
        _normalize_name_for_compare(a.get("Local_name")),
        _normalize_name_for_compare(a.get("shipyard_name")),
    ]
    names_b = [
        _normalize_name_for_compare(b.get("English_name")),
        _normalize_name_for_compare(b.get("Local_name")),
        _normalize_name_for_compare(b.get("shipyard_name")),
    ]

    for na in names_a:
        if not na or len(na) < 2:
            continue
        for nb in names_b:
            if not nb or len(nb) < 2:
                continue
            if na == nb:
                return True
            if len(na) >= 5 and len(nb) >= 5:
                if na in nb or nb in na:
                    return True
    return False


def _choose_better_value(old: Any, new: Any) -> Any:
    """在旧值与新值之间选择更优的一个。"""
    if old is None or str(old).strip() == "":
        return new
    if new is None or str(new).strip() == "":
        return old
    s_old = str(old).strip()
    s_new = str(new).strip()
    if s_old == s_new:
        return old
    # 文本字段优先更详细
    if len(s_new) > len(s_old) * 1.2:
        return new
    return old


def _merge_list_field(old: Any, new: Any) -> Any:
    """合并列表/JSON 字段：非空且不同的列表尝试简单合并（去重）。"""
    if old is None or old == "":
        return new
    if new is None or new == "":
        return old
    if isinstance(old, list) and isinstance(new, list):
        merged = list(old)
        for item in new:
            if item not in merged:
                merged.append(item)
        return merged
    if isinstance(old, str) and isinstance(new, str):
        try:
            old_list = json.loads(old)
            new_list = json.loads(new)
            if isinstance(old_list, list) and isinstance(new_list, list):
                merged = list(old_list)
                for item in new_list:
                    if item not in merged:
                        merged.append(item)
                return json.dumps(merged, ensure_ascii=False)
        except Exception:
            pass
    return _choose_better_value(old, new)


def _rule_based_merge(dicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """基于名称相似性合并同船厂记录。"""
    if not dicts:
        return []

    groups: List[Dict[str, Any]] = []
    for d in dicts:
        merged_into = None
        for g in groups:
            if _same_shipyard(d, g):
                merged_into = g
                break
        if merged_into is None:
            groups.append(dict(d))
        else:
            for key, value in d.items():
                if key in {"source_url", "source_news_link", "url"}:
                    merged_into[key] = _merge_list_field(merged_into.get(key), value)
                elif isinstance(value, (list, dict)) or key in {
                    "production_lines",
                    "crane_parameters",
                    "qualification_certifications",
                    "other_facilities",
                    "main_ship_types_now",
                    "ship_types_delivered",
                }:
                    merged_into[key] = _merge_list_field(merged_into.get(key), value)
                else:
                    merged_into[key] = _choose_better_value(merged_into.get(key), value)

    return groups


def _ai_merge_group(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    """调用 AI 将同一船厂的多条提取结果整合为一条最全面的记录。"""
    if len(group) == 1:
        return dict(group[0])

    # Build a compact summary for the prompt.
    records_json = json.dumps(group, ensure_ascii=False, indent=2)
    prompt = f"""You are a maritime data integration expert.
Merge the following shipyard records, which all refer to the SAME shipyard, into a single comprehensive record.

Rules:
1. Return ONLY a valid JSON object containing the merged shipyard fields.
2. For scalar fields, keep the most detailed / most complete value.
3. For list fields (production_lines, crane_parameters, qualification_certifications, other_facilities), merge and deduplicate items.
4. Preserve all distinct source URLs in a list field named "source_news_link".
5. Do not add fields that are not present in the input records.
6. Do not include any explanation, markdown, or code block.

Records:
{records_json}
"""
    try:
        result_str = call_qwen_api(prompt, "")
        if not result_str:
            return _rule_based_merge(group)[0]

        result_str = result_str.strip()
        if result_str.startswith("```json"):
            result_str = result_str[7:]
        if result_str.startswith("```"):
            result_str = result_str[3:]
        if result_str.endswith("```"):
            result_str = result_str[:-3]
        result_str = result_str.strip()

        merged = json.loads(result_str)
        if isinstance(merged, dict):
            return merged
    except Exception as exc:
        logger.warning("AI intra-batch merge failed, falling back to rule-based merge: %s", exc)

    return _rule_based_merge(group)[0]


def merge_same_shipyard_in_batch(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    对同一批次内重复出现的船厂进行 AI 智能整合。

    步骤：
    1. 按规范化名称粗分组（精确匹配）。
    2. 对每组调用 AI 合并为一条最全面的记录。
    3. 返回合并后的记录列表，每个船厂仅出现一次。
    """
    if not records:
        return []

    # Step 1: coarse grouping by exact normalized Local_name / English_name.
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        local = _clean_alias(rec.get("Local_name"))
        english = _clean_alias(rec.get("English_name"))
        key = local or english or "unknown"
        key = _normalize_name_for_compare(key)
        groups.setdefault(key, []).append(rec)

    # Step 2: merge each group.
    merged_records: List[Dict[str, Any]] = []
    for group in groups.values():
        merged_records.append(_ai_merge_group(group))

    return merged_records


# ========== 4. Visual deduplication against DB ==========

def visualize_deduplicate(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    对单条提取结果做 DB 本地名文本匹配，仅做视觉呈现。

    不写数据库、不写实体词表。
    返回结构与 DedupRecord 一致。
    """
    name = _candidate_name(record)
    if not name:
        return {
            "extracted_data": record,
            "duplicate": False,
            "canonical_name": None,
            "replacement": None,
            "existing_record": None,
            "is_new_entity": False,
            "same_fields": {},
        }

    canonical_name = _clean_alias(record.get("canonical_name")) or name
    existing_record = find_db_match(canonical_name)
    if existing_record is None:
        existing_record = find_db_match(name)

    duplicate = existing_record is not None
    replacement = None
    same_fields: Dict[str, bool] = {}

    if existing_record:
        existing_record = _serialize_row(existing_record)
        replacement = dict(record)
        for key, value in existing_record.items():
            if value is not None:
                replacement[key] = value

    return {
        "extracted_data": record,
        "duplicate": duplicate,
        "canonical_name": canonical_name if duplicate else None,
        "replacement": replacement,
        "existing_record": existing_record,
        "is_new_entity": False,
        "same_fields": same_fields,
    }


def batch_visualize_deduplicate(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    批量可视化去重：对多条提取结果只做一次 DB 查询，显著降低数据库压力。

    返回与输入顺序对应的 DedupRecord 列表。
    """
    if not records:
        return []

    # Collect names to query.
    names = []
    for record in records:
        name = _candidate_name(record)
        canonical = _clean_alias(record.get("canonical_name")) or name
        if canonical:
            names.append(canonical)
        if name and name != canonical:
            names.append(name)

    # Single batch DB lookup.
    db_matches = batch_find_db_matches(names) if names else {}

    results = []
    for record in records:
        name = _candidate_name(record)
        canonical_name = _clean_alias(record.get("canonical_name")) or name

        existing_record = None
        if canonical_name:
            existing_record = db_matches.get(canonical_name)
        if existing_record is None and name and name != canonical_name:
            existing_record = db_matches.get(name)

        duplicate = existing_record is not None
        replacement = None
        if existing_record:
            replacement = dict(record)
            for key, value in existing_record.items():
                if value is not None:
                    replacement[key] = value

        results.append({
            "extracted_data": record,
            "duplicate": duplicate,
            "canonical_name": canonical_name if duplicate else None,
            "replacement": replacement,
            "existing_record": existing_record,
            "is_new_entity": False,
            "same_fields": {},
        })

    return results

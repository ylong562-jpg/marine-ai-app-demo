"""
字段校验与重生成流水线。

基于 field_specs 对 AI 提取结果进行：
1. 标准化（normalize）
2. 校验（validate）
3. 对失败字段调用轻量 LLM Prompt 重生成（regenerate）
4. 默认值兜底
"""
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.shipyard_ai.field_specs import (
    FieldSpec,
    FORMAT_REGISTRY,
    get_field_specs,
    get_field_spec,
    normalize_field,
    validate_field,
)
from app.shipyard_ai.prompt_builder import build_regeneration_prompt


def _strip_markdown_json(text: str) -> str:
    """去掉 LLM 返回的 markdown 代码块标记。"""
    if not text:
        return ""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _parse_llm_json(text: str) -> Optional[dict]:
    """安全解析 LLM 返回的 JSON。"""
    text = _strip_markdown_json(text)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """对记录中所有已知字段按规范做标准化。"""
    specs = get_field_specs()
    out = dict(record)
    for spec in specs:
        if spec.name not in out or out[spec.name] is None:
            continue
        out[spec.name] = normalize_field(spec, out[spec.name])
    return out


def validate_record(record: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Tuple[FieldSpec, str]]]:
    """
    校验记录中的字段。
    返回值：(标准化后的记录, 失败字段列表[(spec, error), ...])。
    """
    normalized = normalize_record(record)
    failed: List[Tuple[FieldSpec, str]] = []
    for spec in get_field_specs():
        value = normalized.get(spec.name)
        if value is None:
            continue
        ok, err = validate_field(spec, value)
        if not ok:
            failed.append((spec, err))
    return normalized, failed


def _derive_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """根据已有字段推导衍生字段。"""
    out = dict(record)
    # founded_year 可由 establishment_date 推导
    if out.get("founded_year") is None and out.get("establishment_date"):
        try:
            m = re.match(r"^(\d{4})", str(out["establishment_date"]))
            if m:
                out["founded_year"] = int(m.group(1))
        except Exception:
            pass
    return out


def apply_defaults(record: Dict[str, Any]) -> Dict[str, Any]:
    """对仍未通过校验或缺失的关键字段应用默认值。"""
    out = dict(record)
    for spec in get_field_specs():
        if spec.default is None:
            continue
        value = out.get(spec.name)
        if value is None or value == "":
            out[spec.name] = spec.default
    return out


def regenerate_failed_fields(
    introduce_text: str,
    record: Dict[str, Any],
    failed_specs: List[FieldSpec],
    llm_call: Callable[[str, str], Optional[str]],
    max_attempts: int = 2,
) -> Dict[str, Any]:
    """
    对校验失败的字段调用轻量 LLM Prompt 重生成，最多 max_attempts 次。
    llm_call: (prompt, text) -> response_str
    """
    out = dict(record)
    if not failed_specs:
        return out

    current_failed = list(failed_specs)
    for attempt in range(1, max_attempts + 1):
        prompt = build_regeneration_prompt(current_failed, out, introduce_text)
        response = llm_call(prompt, introduce_text)
        if not response:
            print(f"⚠️ 字段重生成第 {attempt} 次无返回")
            continue

        parsed = _parse_llm_json(response)
        if not isinstance(parsed, dict):
            print(f"⚠️ 字段重生成第 {attempt} 次解析 JSON 失败")
            continue

        # 合并重生成结果
        for spec in current_failed:
            if spec.name in parsed:
                out[spec.name] = parsed[spec.name]

        out = normalize_record(out)

        # 再次校验
        still_failed = []
        for spec in current_failed:
            value = out.get(spec.name)
            if value is None:
                continue
            ok, err = validate_field(spec, value)
            if not ok:
                still_failed.append((spec, err))

        print(f"✓ 字段重生成第 {attempt} 次：{len(current_failed) - len(still_failed)}/{len(current_failed)} 个字段已修复")
        if not still_failed:
            break
        current_failed = [spec for spec, _ in still_failed]

    return out


def _detect_normalization_failures(record: Dict[str, Any]) -> Tuple[Dict[str, Any], List[FieldSpec]]:
    """
    检测那些“有值但无法标准化成合法格式”的字段。
    例如 establishment_date='not a date'、shipyard_area='unknown' 等。
    返回 (已尽可能标准化的记录, 标准化失败的字段列表)。
    """
    out = dict(record)
    failed: List[FieldSpec] = []
    for spec in get_field_specs():
        value = out.get(spec.name)
        if value is None or value == "":
            continue
        # 自由文本与自定义字段不在这里强制重生成
        if spec.format_type in ("text", "custom"):
            continue
        normalized = normalize_field(spec, value)
        if normalized is None:
            failed.append(spec)
        else:
            out[spec.name] = normalized
    return out, failed


def run_validation_pipeline(
    introduce_text: str,
    record: Dict[str, Any],
    llm_call: Callable[[str, str], Optional[str]],
    skip_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    完整流水线：检测无法标准化的字段 → 重生成 → 标准化 → 校验 → 重生成 →
    再校验 → 推导字段 → 默认值兜底。

    skip_fields: 跳过通用校验/重生成的字段名（例如 production_lines / annual_production_capacity，
                 它们由调用方用更专门的处理逻辑负责）。
    """
    skip = set(skip_fields or [])

    # 阶段 1：检测并修复无法标准化的字段
    out, norm_failed = _detect_normalization_failures(record)
    norm_failed = [spec for spec in norm_failed if spec.name not in skip]
    if norm_failed:
        print(f"⚠️ 字段无法标准化，尝试重生成：{[s.name for s in norm_failed]}")
        out = regenerate_failed_fields(introduce_text, out, norm_failed, llm_call)

    # 阶段 2：再次标准化并校验
    out = normalize_record(out)
    normalized, failed = validate_record(out)
    out = normalized
    failed = [(spec, err) for spec, err in failed if spec.name not in skip]

    if failed:
        failed_specs = [spec for spec, _ in failed]
        print(f"⚠️ 字段校验失败，尝试重生成：{[s.name for s in failed_specs]}")
        out = regenerate_failed_fields(introduce_text, out, failed_specs, llm_call)
        out = normalize_record(out)
        normalized2, failed2 = validate_record(out)
        out = normalized2
        failed2 = [(spec, err) for spec, err in failed2 if spec.name not in skip]
        if failed2:
            print(f"⚠️ 字段重生成后仍失败：{[(s.name, e) for s, e in failed2]}")
        else:
            print("✓ 所有字段已通过校验")
    else:
        print("✓ 所有字段格式正确")

    out = _derive_fields(out)
    out = apply_defaults(out)
    return out

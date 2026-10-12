"""
动态 Prompt 构建器。

所有船厂提取 Prompt 都基于 field_specs 中的字段规范生成，避免在多个
extract_* 函数中硬重复字段说明和示例。
"""
import json
from typing import List

from app.shipyard_ai.field_specs import FieldSpec, get_stage_fields


_SYSTEM_ROLE = (
    "You are a professional maritime industry data analyst specialized in extracting "
    "structured information from shipyard descriptions. Always respond with valid JSON format "
    "with English content only, except where Chinese is explicitly requested."
)


def _format_type_hint(spec: FieldSpec) -> str:
    """根据字段规范生成格式提示。"""
    hints = {
        "date": "format YYYY-MM-DD",
        "yn": 'MUST be exactly "Y" or "N"',
        "enum": f"MUST be exactly one of {spec.allowed_values}",
        "pure_number": "pure number only, no units or commas",
        "semicolon_list": "items separated by semicolons",
        "json_object": "valid JSON object",
        "json_array": "valid JSON array",
        "int": "integer",
        "decimal": "decimal number",
        "year": "4-digit year",
    }
    return hints.get(spec.format_type, "string")


def _field_instruction(spec: FieldSpec, index: int) -> str:
    lines = [f"{index}. {spec.name}: {spec.description}"]
    hint = _format_type_hint(spec)
    lines.append(f"   - Format: {hint}")
    if spec.example is not None:
        example_str = json.dumps(spec.example, ensure_ascii=False)
        lines.append(f"   - Example: {example_str}")
    if spec.extra_prompt:
        lines.append(f"   - Rule: {spec.extra_prompt}")
    return "\n".join(lines)


def _json_example(stage_specs: List[FieldSpec]) -> dict:
    """生成该阶段 JSON 输出示例。"""
    example = {}
    for spec in stage_specs:
        if spec.example is not None:
            example[spec.name] = spec.example
        else:
            example[spec.name] = None
    return example


def _stage_preamble(stage: str) -> str:
    """阶段特定前置说明。"""
    if stage == "basic":
        return (
            "CRITICAL RULES:\n"
            "- Local_name MUST be the accurate official Chinese name. If you only see an English name in the text, "
            "use your knowledge to find the exact Chinese name. Do NOT guess or translate literally.\n"
            "- former_name MUST be returned in Chinese characters if it refers to a Chinese shipyard name.\n"
            "- ALL OTHER field values MUST be returned in professional English terminology ONLY. "
            "Do NOT return Chinese for any field other than Local_name and former_name.\n"
            "- Abreviation should be short (2-5 uppercase letters), based on the English name initials.\n"
            "- establishment_date must be YYYY-MM-DD; if only a year is available, use YYYY-01-01.\n"
            "- Return only valid JSON format, no additional explanatory text."
        )
    if stage == "qualifications":
        return (
            "CRITICAL RULES:\n"
            "- All field values must be returned in professional English terminology only.\n"
            "- annual_production_capacity must be a JSON object with exactly two keys: \"vessel\" and \"specs\".\n"
            "  * vessel: annual number of vessels built (string). Use a range like \"10-20\" or a single number like \"10\". Use null if not mentioned.\n"
            "  * specs: annual total production capacity in DWT (number). Convert \"million DWT\", \"thousand DWT\" etc. to a pure number. Use null if not mentioned.\n"
            "- qualification_certifications should use semicolons to separate multiple items.\n"
            "- Return only JSON, no additional explanatory text."
        )
    if stage == "scale":
        return (
            "CRITICAL RULES:\n"
            "- Numbers should not contain commas and units, return pure numbers only.\n"
            "- All field values must be returned in professional English terminology only.\n"
            "- If no clear information is found in the text, fill in null.\n"
            "- Return only JSON, no additional explanatory text."
        )
    if stage == "facilities":
        return (
            "CRITICAL STRUCTURE FOR production_lines - FOLLOW EXACTLY:\n"
            "Each production line MUST contain these fields:\n"
            "- no: integer, line number (e.g., 1, 2, 3)\n"
            "- type: string, MUST be exactly ONE of [\"slipway\", \"dry dock\"]. DO NOT use any other value.\n"
            "  * \"dry dock\": enclosed dock with gates, typically has depth dimension (L×W×D)\n"
            "  * \"slipway\": inclined surface for ship launching, typically only length×width\n"
            "- capacity: number ONLY, no unit. Unit is ton by default. (e.g., 30000, not \"30000 tons\")\n"
            "- dimensions: string with numbers separated by \" × \", no units. Unit is meter (m) by default.\n"
            "  * ONLY use dimensions explicitly stated in the text. If not mentioned, return null.\n"
            "  * For dry dock, ALWAYS include depth if explicitly mentioned. If depth is not mentioned, use \"[length] × [width]\".\n"
            "  * For slipway, use \"[length] × [width]\" (no depth).\n"
            "- primary_function: string, the main use/purpose of this production line (e.g., \"VLCC construction\", \"container ship\", \"LNG carrier\", \"ship repair\", \"general cargo\"). Use English. Set to null if not mentioned in the text.\n"
            "- is_active: string, MUST be exactly \"Y\" or \"N\". Y = currently active and operating. N = closed, dismantled, or no longer in use. If uncertain, default to \"Y\".\n"
            "- cranes: ARRAY of crane objects. EACH production line can have MULTIPLE cranes assigned to it. The total number of cranes across all production_lines MUST equal the total number of individual cranes parsed from crane_parameters.\n"
            "  * Each crane object contains: no (integer, globally unique), type (\"gantry crane\" or \"normal crane\"), capacity (number ONLY, no unit).\n\n"
            "CRANE-TO-LINE ASSIGNMENT RULES:\n"
            "1. Parse crane_parameters into individual cranes. Example: \"2×900t gantry cranes\" → 2 cranes of 900t each.\n"
            "2. Distribute cranes across production_lines. Larger lines (by dimensions, then capacity) get more and stronger cranes.\n"
            "3. The cranes field MUST be an array, even if a line has only 1 crane.\n"
            "4. If the text only gives a TOTAL lifting capacity without breaking it down, put ONE crane with that capacity or leave cranes empty. DO NOT invent multiple cranes.\n\n"
            "CRITICAL DISTINCTION RULES - FOLLOW STRICTLY:\n"
            "1. **production_lines** (Shipways/Building Docks):\n"
            "   - These are ship construction facilities (船坞/船台/造船坞)\n"
            "   - Capacity is typically in the THOUSAND tons range: 10000 to 500000 (e.g., 600, 1500, 3000, 30000)\n"
            "   - Dimensions MAY be mentioned as length × width × depth. If not mentioned, set dimensions to null.\n"
            "   - Usually numbered as \"No.1\", \"No.2\" shipway/dock\n"
            "   - **UNIT RULE**: capacity and dimensions MUST be returned as PURE NUMBERS ONLY, without any unit suffix. Default unit for capacity is ton, for dimensions is meter (m).\n\n"
            "2. **crane_parameters** (Cranes/Gantries):\n"
            "   - These are lifting equipment (门吊/龙门吊/浮吊)\n"
            "   - Capacity is typically 100t to 2000t range: 100, 300, 600, 900, 1000, etc.\n"
            "   - Usually described as \"X×Yt gantry cranes\" or \"X×Yt floating crane\"\n"
            "   - Key difference: NO dimensions (L×W×H), only quantity × tonnage + crane type\n"
            "   - **UNIT RULE**: keep the original format like \"2×900t gantry cranes\" for this field.\n\n"
            "3. **other_facilities**:\n"
            "   - Floating cranes and semi-submersible barges with large capacity (e.g., \"3000t floating crane\", \"38000t barge\") belong here, NOT in production_lines or crane_parameters.\n\n"
            "NUMERIC THRESHOLD GUIDE:\n"
            "- If capacity is around 100-5000 AND has dimensions → production_lines\n"
            "- If capacity is 100+ (especially 300, 600, 900, 1000+) AND described as cranes → crane_parameters\n"
            "- When in doubt: production_lines have dimensions (L×W×H), crane_parameters do NOT have dimensions.\n\n"
            "ANTI-HALLUCINATION RULES:\n"
            "- ALL numeric values MUST be explicitly found in the source text.\n"
            "- DO NOT copy numbers from the example below.\n"
            "- If dimensions are not mentioned, set dimensions to null.\n"
            "- If individual crane capacities are not mentioned, set cranes[].capacity to null.\n"
            "- It is BETTER to return null than to invent a number.\n"
            "- All field values MUST be returned in professional English terminology ONLY. Do NOT return Chinese.\n"
            "- Return only JSON, no additional explanatory text."
        )
    return ""


def build_extraction_prompt(stage: str, introduce_text: str) -> str:
    """为指定阶段生成完整提取 Prompt。"""
    specs = get_stage_fields(stage)
    if not specs:
        raise ValueError(f"Unknown or empty stage: {stage}")

    lines = [
        "Please extract the following information from the shipyard introduction text below and return in JSON format.",
        "If a field cannot be found in the text, please fill in null.",
        "",
        "Fields to extract:",
    ]
    for idx, spec in enumerate(specs, start=1):
        lines.append(_field_instruction(spec, idx))

    example = _json_example(specs)
    preamble = _stage_preamble(stage)

    prompt = "\n".join(lines) + "\n\n"
    prompt += f"Return format example:\n{json.dumps(example, ensure_ascii=False, indent=2)}\n\n"
    prompt += preamble
    return prompt


def build_regeneration_prompt(failed_specs: List[FieldSpec], current_values: dict, introduce_text: str) -> str:
    """为校验失败的字段生成轻量重生成 Prompt。"""
    lines = [
        "The previous extraction returned invalid values for the following fields. "
        "Please re-extract ONLY these fields from the shipyard introduction text below and return STRICTLY in JSON format.",
        "",
        "Fields to re-extract:",
    ]
    for idx, spec in enumerate(failed_specs, start=1):
        lines.append(_field_instruction(spec, idx))

    lines.extend([
        "",
        "Current problematic values (for reference only):",
        json.dumps({spec.name: current_values.get(spec.name) for spec in failed_specs}, ensure_ascii=False, indent=2),
        "",
        "Return ONLY valid JSON (no markdown, no explanation):",
        json.dumps(_json_example(failed_specs), ensure_ascii=False, indent=2),
    ])
    return "\n".join(lines)

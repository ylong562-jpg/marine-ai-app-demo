
import json
import os
import re
import random
import string
from typing import Dict, Any, Optional
import dashscope
from http import HTTPStatus

from app.shipyard_ai import prompt_builder, validation
from app.shipyard_ai.field_specs import _classify_group_by_text, _normalize_group
from app.shipyard_ai.validation import _parse_llm_json

# Configure DashScope API from environment variable
dashscope.api_key = os.getenv('DASHSCOPE_API_KEY')


def call_qwen_api(prompt: str, text: str, model: str = 'qwen-plus', timeout: int = 120) -> Optional[str]:
    """
    Call Qwen large model API with a hard timeout to avoid indefinite hangs.

    Args:
        prompt: system/user prompt
        text: text to analyze
        model: model name
        timeout: maximum seconds to wait for the API response (default 120)

    Returns:
        API response string or None on failure/timeout
    """
    def _call():
        messages = [
            {'role': 'system', 'content': prompt_builder._SYSTEM_ROLE},
            {'role': 'user', 'content': f"{prompt}\n\nText to analyze:\n{text}"}
        ]

        response = dashscope.Generation.call(
            model=model,
            messages=messages,
            result_format='message',
            temperature=0.1,
            top_p=0.8,
            request_timeout=timeout,
        )

        if response.status_code == HTTPStatus.OK:
            return response['output']['choices'][0]['message']['content']
        else:
            print(f'API call failed: {response.code} - {response.message}')
            return None

    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            return future.result(timeout=timeout)
    except FutureTimeoutError:
        print(f'API call timed out after {timeout} seconds')
        return None
    except Exception as e:
        print(f'API call exception: {e}')
        return None


def _llm_call(prompt: str, text: str) -> Optional[str]:
    """适配 validation 模块回调签名的轻量包装。"""
    return call_qwen_api(prompt, text)


def extract_basic_info(introduce_text: str) -> Dict[str, Any]:
    """
    Extract basic information: English name, local name, abbreviation, former name, shipyard ID, location, establishment date, country, state-owned status, group, UBO
    """
    prompt = prompt_builder.build_extraction_prompt('basic', introduce_text)

    result_str = call_qwen_api(prompt, introduce_text)
    if result_str:
        result = _parse_llm_json(result_str)
        if not result:
            print('JSON parsing failed for basic_info')
            return {}
        # 集团字段：优先根据原文本判断；若无法判断，再使用 LLM 输出兜底；最终规范化为四个选项之一
        text_classified = _classify_group_by_text(introduce_text)
        llm_group = result.get("group")
        if text_classified:
            result["group"] = text_classified
        elif llm_group:
            result["group"] = _normalize_group(llm_group)
        else:
            result["group"] = "其它"
        return result
    return {}


def _parse_vessel_range(text):
    """从文本中提取年造船数量范围，返回 'min-max' 或单个数字字符串，无则 None。"""
    if not text:
        return None
    s = text.lower()

    has_vessel_indicator = re.search(r'\b(vessels?|ships?|boats?|crafts?|units?)\b', s, re.IGNORECASE) is not None
    has_chinese_vessel = '艘' in s
    is_pure_range = re.match(r'^\s*\d+\s*[-~–—]\s*\d+\s*$', s) or re.match(r'^\s*\d+\s*$', s)
    if not has_vessel_indicator and not has_chinese_vessel and not is_pure_range:
        return None

    m = re.search(r'(\d+)\s*(?:to|[-~–—])\s*(\d+)', s, re.IGNORECASE)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a == b:
            return str(a)
        return f"{min(a, b)}-{max(a, b)}"

    m = re.search(r'(\d+)', s)
    if m:
        return m.group(1)

    return None


def _parse_dwt_number(text):
    """从文本中提取 DWT 数值（统一为纯数字），无则 None。"""
    if not text:
        return None
    s = text.lower()

    # 优先匹配紧跟 DWT/吨单位的数值，避免误取前文的 vessel 数量
    patterns = [
        r'([\d,\.]+)\s*(million|m|billion|bn|thousand|k|万|亿)?\s*(?:dwt|deadweight|tons?|tonnes?|吨)',
        r'([\d,\.]+)\s*(?:dwt|deadweight|tons?|tonnes?|吨)',
        r'([\d,\.]+)\s*(million|m|billion|bn|thousand|k|万|亿)\s*(?:dwt|deadweight|tons?|tonnes?|吨)?',
    ]
    for pattern in patterns:
        m = re.search(pattern, s, re.IGNORECASE)
        if m:
            num_str = m.group(1).replace(',', '')
            multiplier_str = (m.group(2) or '').lower()
            try:
                num = float(num_str)
            except ValueError:
                continue

            multiplier = 1
            if multiplier_str in ('million', 'm'):
                multiplier = 1_000_000
            elif multiplier_str in ('billion', 'bn', 'b'):
                multiplier = 1_000_000_000
            elif multiplier_str in ('thousand', 'k'):
                multiplier = 1_000
            elif multiplier_str == '万':
                multiplier = 10_000
            elif multiplier_str == '亿':
                multiplier = 100_000_000

            result = num * multiplier
            if result == int(result):
                return int(result)
            return result
    return None


def _validate_annual_capacity_obj(obj):
    """规范化并校验 annual_production_capacity 对象，确保字段类型正确。"""
    if not isinstance(obj, dict):
        return {"vessel": None, "specs": None, "_raw": str(obj) if obj is not None else None}

    vessel = obj.get('vessel')
    specs = obj.get('specs')

    if vessel is not None:
        vessel_str = str(vessel).strip()
        parsed = _parse_vessel_range(vessel_str)
        if parsed:
            vessel = parsed
        elif re.match(r'^\d+(?:[-~–—]\d+)?$', vessel_str):
            vessel = vessel_str
        else:
            vessel = None

    if specs is not None:
        if isinstance(specs, (int, float)):
            specs = int(specs) if specs == int(specs) else specs
        else:
            parsed = _parse_dwt_number(str(specs))
            specs = parsed

    result = {"vessel": vessel, "specs": specs}
    raw = obj.get('_raw') or obj.get('raw')
    if raw:
        result['_raw'] = raw
    elif vessel is None and specs is None:
        result['_raw'] = json.dumps(obj, ensure_ascii=False)
    return result


def _normalize_annual_production_capacity(cap):
    """
    将 annual_production_capacity 统一规范化为 {"vessel": "..." | null, "specs": number | null}。
    输入可以是模型返回的字符串、JSON 对象或旧库中的自由文本。
    无法解析时保留 _raw 字段用于人工兜底。
    """
    if isinstance(cap, dict):
        return _validate_annual_capacity_obj(cap)

    if not isinstance(cap, str) or not cap.strip():
        return {"vessel": None, "specs": None}

    raw = cap.strip()
    s = raw.lower()

    redundant_prefixes = [
        r'(?i)^annual\s+production\s+(capacity\s+)?of\s+',
        r'(?i)^annual\s+capacity\s+of\s+',
        r'(?i)^production\s+capacity\s*[:：]?\s*',
        r'(?i)^capacity\s*[:：]?\s*',
        r'(?i)^with\s+an\s+annual\s+',
        r'(?i)^with\s+a\s+production\s+capacity\s+of\s+',
        r'(?i)^capable\s+of\s+producing\s+',
        r'(?i)^can\s+produce\s+',
    ]
    for pattern in redundant_prefixes:
        s = re.sub(pattern, '', s)
    s = s.strip()

    vessel = _parse_vessel_range(s)
    specs = _parse_dwt_number(s)

    result = {"vessel": vessel, "specs": specs}
    if vessel is None and specs is None:
        result["_raw"] = raw
    return result


def extract_qualifications_and_capacity(introduce_text: str) -> Dict[str, Any]:
    """
    Extract qualification certificates, refund guarantee capacity, annual production capacity
    """
    prompt = prompt_builder.build_extraction_prompt('qualifications', introduce_text)

    result_str = call_qwen_api(prompt, introduce_text)
    if result_str:
        result = _parse_llm_json(result_str)
        if not result:
            print('JSON parsing failed for qualifications_and_capacity')
            return {}
        if 'annual_production_capacity' in result:
            result['annual_production_capacity'] = _normalize_annual_production_capacity(result['annual_production_capacity'])
        return result
    return {}


def extract_shipyard_scale(introduce_text: str) -> Dict[str, Any]:
    """
    Extract shipyard scale: area, quayside length
    """
    prompt = prompt_builder.build_extraction_prompt('scale', introduce_text)

    result_str = call_qwen_api(prompt, introduce_text)
    if result_str:
        result = _parse_llm_json(result_str)
        if not result:
            print('JSON parsing failed for shipyard_scale')
            return {}
        return result
    return {}


def _extract_pure_number(value):
    """从字符串中提取纯数字，去掉单位和逗号"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    # 匹配开头的数字（支持逗号分隔和空格）
    m = re.match(r'^([\d\s,\.]+)', s)
    if m:
        num_str = m.group(1).replace(',', '').replace(' ', '')
        try:
            num = float(num_str)
            return int(num) if num == int(num) else num
        except ValueError:
            pass
    return None


def _normalize_dimensions(dim_str):
    """将 dimensions 字符串处理为纯数字格式（空格分隔，无单位）"""
    if not dim_str:
        return None
    s = str(dim_str).strip()
    # 处理乱码/替代字符为 "×"
    s = s.replace('��', '×')
    # 移除所有单位字符 (m, meters, metres, ft, feet, yards, yd)
    s = re.sub(r'\s*(?:meters?|metres?|m|ft|feet|yards?|yd)\b', '', s, flags=re.IGNORECASE)
    # 统一分隔符为 " × "
    s = re.sub(r'\s*[xX×*]\s*', ' × ', s)
    # 清理多余空格
    s = re.sub(r'\s+', ' ', s).strip()
    return s if s else None


def _validate_production_lines(lines):
    """
    验证 production_lines 的 type 和 cranes[].type 是否严格符合要求。
    如果不符合，强制修正或标记为需要重新生成。
    """
    VALID_LINE_TYPES = {'slipway', 'dry dock', 'land assembly platform'}
    VALID_CRANE_TYPES = {'gantry crane', 'normal crane'}

    if not isinstance(lines, list):
        return lines

    for line in lines:
        if not isinstance(line, dict):
            continue
        # 验证并修正外层 type
        line_type = line.get('type', '').strip().lower()
        if line_type not in VALID_LINE_TYPES:
            if 'dock' in line_type or '坞' in line_type:
                line['type'] = 'dry dock'
            elif 'slip' in line_type or 'way' in line_type or '滑道' in line_type:
                line['type'] = 'slipway'
            elif 'land' in line_type or '陆地' in line_type or '总装' in line_type:
                line['type'] = 'land assembly platform'
            else:
                dim = line.get('dimensions', '')
                if dim and ('×' in str(dim) or 'x' in str(dim).lower()):
                    parts = str(dim).replace(' ', '').split('×')
                    if len(parts) >= 3:
                        line['type'] = 'dry dock'
                    else:
                        line['type'] = 'slipway'
                else:
                    line['type'] = 'dry dock'

        # 验证并修正 cranes 数组中每个 crane 的 type
        cranes = line.get('cranes')
        if isinstance(cranes, list):
            for crane in cranes:
                if isinstance(crane, dict):
                    crane_type = str(crane.get('type', '')).strip().lower()
                    if crane_type not in VALID_CRANE_TYPES:
                        if 'gantry' in crane_type or '龙门' in crane_type or '门式' in crane_type:
                            crane['type'] = 'gantry crane'
                        else:
                            crane['type'] = 'normal crane'
        elif 'crane' in line:
            # 兼容旧格式：单数 crane 对象转 cranes 数组
            old_crane = line.pop('crane')
            if isinstance(old_crane, dict):
                line['cranes'] = [old_crane]
            else:
                line['cranes'] = []
        elif cranes is None:
            line['cranes'] = []

    return lines


def _normalize_production_lines(lines):
    """
    规范化 production_lines：
    - capacity 转为纯数字（单位默认 ton）
    - dimensions 转为纯数字格式（单位默认 m，用 × 分隔）
    - cranes[].capacity 转为纯数字（单位默认 t）
    - 验证 type 字段的合法性
    """
    if not isinstance(lines, list):
        return lines
    for line in lines:
        if not isinstance(line, dict):
            continue
        # capacity -> 纯数字（ton）
        cap = line.get('capacity')
        if cap is not None:
            pure = _extract_pure_number(cap)
            if pure is not None:
                line['capacity'] = pure

        # dimensions -> 纯数字格式（m），用 " × " 分隔
        dim = line.get('dimensions')
        if dim is not None:
            normalized = _normalize_dimensions(dim)
            if normalized is not None:
                line['dimensions'] = normalized

        # primary_function -> 字符串或 null
        if 'primary_function' not in line:
            line['primary_function'] = None
        else:
            pf = line['primary_function']
            if pf is not None:
                pf = str(pf).strip()
                line['primary_function'] = pf if pf else None

        # is_active -> "Y" 或 "N"，默认 "Y"
        ia = line.get('is_active')
        if isinstance(ia, bool):
            line['is_active'] = 'Y' if ia else 'N'
        else:
            ia_str = str(ia).strip().upper() if ia is not None else ''
            if ia_str in ('Y', 'YES', 'TRUE', '1'):
                line['is_active'] = 'Y'
            elif ia_str in ('N', 'NO', 'FALSE', '0'):
                line['is_active'] = 'N'
            else:
                line['is_active'] = 'Y'

        # cranes[].capacity -> 纯数字（t）
        cranes = line.get('cranes')
        if isinstance(cranes, list):
            for idx, crane in enumerate(cranes):
                if isinstance(crane, dict):
                    c_cap = crane.get('capacity')
                    if c_cap is not None:
                        pure = _extract_pure_number(c_cap)
                        if pure is not None:
                            crane['capacity'] = pure
                    # crane.no 确保为整数
                    c_no = crane.get('no')
                    if c_no is not None:
                        try:
                            crane['no'] = int(c_no)
                        except (ValueError, TypeError):
                            crane['no'] = idx + 1
        # 兼容旧格式：单数 crane 转 cranes
        elif 'crane' in line:
            old_crane = line.pop('crane')
            if isinstance(old_crane, dict):
                c_cap = old_crane.get('capacity')
                if c_cap is not None:
                    pure = _extract_pure_number(c_cap)
                    if pure is not None:
                        old_crane['capacity'] = pure
                line['cranes'] = [old_crane]
            else:
                line['cranes'] = []

        # no 确保为整数
        line_no = line.get('no')
        if line_no is not None:
            try:
                line['no'] = int(line_no)
            except (ValueError, TypeError):
                line['no'] = 1

    # 最终验证
    lines = _validate_production_lines(lines)
    return lines


def _normalize_crane_parameters(crane_str):
    """规范化 crane_parameters 的载重单位为 t"""
    if not isinstance(crane_str, str) or not crane_str.strip():
        return crane_str
    s = crane_str.strip()
    # 将 tons/tonnes/ton 替换为 t（紧跟在数字后面）
    s = re.sub(r'(\d)\s*(?:tons?|tonnes?)\b', r'\1t', s, flags=re.IGNORECASE)
    # 将数字后的空格+大写 T 替换为 t（避免替换非单位T）
    s = re.sub(r'(\d)\s*T(?=\s|$|[^a-zA-Z])', r'\1t', s)
    return s


def _strict_validate_production_lines(lines):
    """
    严格校验 production_lines 是否符合标准格式。
    返回 (is_valid: bool, error_message: str)
    """
    if not isinstance(lines, list):
        return False, f"production_lines 必须是 list，当前类型: {type(lines).__name__}"

    if not lines:
        return False, "production_lines 为空列表"

    VALID_LINE_TYPES = {'slipway', 'dry dock', 'land assembly platform'}
    VALID_CRANE_TYPES = {'gantry crane', 'normal crane'}
    required_line_keys = {'no', 'type', 'cranes', 'capacity', 'dimensions'}
    required_crane_keys = {'no', 'type', 'capacity'}
    crane_nos = set()

    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            return False, f"production_lines[{idx}] 不是 dict"

        missing = required_line_keys - set(line.keys())
        if missing:
            return False, f"production_lines[{idx}] 缺少字段: {missing}"

        line_type = str(line.get('type', '')).strip().lower()
        if line_type not in VALID_LINE_TYPES:
            return False, f"production_lines[{idx}].type 无效: '{line.get('type')}'"

        cranes = line.get('cranes')
        if not isinstance(cranes, list):
            return False, f"production_lines[{idx}].cranes 必须是 list"

        for cidx, crane in enumerate(cranes):
            if not isinstance(crane, dict):
                return False, f"production_lines[{idx}].cranes[{cidx}] 不是 dict"

            missing = required_crane_keys - set(crane.keys())
            if missing:
                return False, f"production_lines[{idx}].cranes[{cidx}] 缺少字段: {missing}"

            crane_type = str(crane.get('type', '')).strip().lower()
            if crane_type not in VALID_CRANE_TYPES:
                return False, f"production_lines[{idx}].cranes[{cidx}].type 无效: '{crane.get('type')}'"

            c_no = crane.get('no')
            try:
                c_no = int(c_no)
            except (ValueError, TypeError):
                return False, f"production_lines[{idx}].cranes[{cidx}].no 不是整数: '{c_no}'"

            if c_no in crane_nos:
                return False, f"crane no 重复: {c_no}"
            crane_nos.add(c_no)

            c_cap = crane.get('capacity')
            if c_cap is not None and not isinstance(c_cap, (int, float)):
                return False, f"production_lines[{idx}].cranes[{cidx}].capacity 必须是数字: '{c_cap}'"

        cap = line.get('capacity')
        if cap is not None and not isinstance(cap, (int, float)):
            return False, f"production_lines[{idx}].capacity 必须是数字: '{cap}'"

        ia = line.get('is_active')
        if ia is not None and str(ia).strip().upper() not in ('Y', 'N'):
            return False, f"production_lines[{idx}].is_active 必须是 Y 或 N: '{ia}'"

        pf = line.get('primary_function')
        if pf is not None and not isinstance(pf, str):
            return False, f"production_lines[{idx}].primary_function 必须是字符串或 null: '{pf}'"

    # 检查 crane no 是否从 1 开始连续
    expected_nos = set(range(1, len(crane_nos) + 1))
    if crane_nos != expected_nos:
        return False, f"crane no 不连续: 实际 {sorted(crane_nos)}，期望 {sorted(expected_nos)}"

    return True, ""


def _parse_crane_parameters(crane_str):
    """
    将 crane_parameters 字符串解析为单个吊机列表。
    支持：2×900t gantry cranes、4×300t、450t + 150t、1×900t+4×300t
    返回：[{"capacity": 900, "type": "gantry crane"}, ...]
    """
    if not isinstance(crane_str, str) or not crane_str.strip():
        return []

    s = crane_str.strip()
    # 统一分隔符和大小写
    s = s.replace('＋', '+').replace('；', ';').replace(',', ';')
    s = re.sub(r'\s*[+;]\s*', ';', s)
    s = re.sub(r'\s*[xX×*]\s*', '×', s)

    cranes = []
    # 模式1: 数量×容量+单位 [类型]
    pattern1 = re.compile(r'(?:(\d+)\s*×\s*)?(\d+(?:\.\d+)?)\s*(?:t|T|tons?|tonnes?)\s*(gantry\s*crane|normal\s*crane|floating\s*crane|crane)?', re.IGNORECASE)
    # 模式2: 容量+单位（无数量）
    pattern2 = re.compile(r'(\d+(?:\.\d+)?)\s*(?:t|T|tons?|tonnes?)\s*(gantry\s*crane|normal\s*crane|floating\s*crane|crane)?', re.IGNORECASE)

    parts = [p.strip() for p in s.split(';') if p.strip()]

    for part in parts:
        matches = list(pattern1.finditer(part))
        if not matches:
            matches = list(pattern2.finditer(part))

        for m in matches:
            qty_str = m.group(1)
            cap_str = m.group(2)
            type_str = m.group(3)

            try:
                capacity = int(float(cap_str))
            except ValueError:
                continue

            qty = 1
            if qty_str:
                try:
                    qty = int(qty_str)
                except ValueError:
                    qty = 1

            crane_type = 'gantry crane'
            if type_str:
                t = type_str.lower()
                if 'normal' in t:
                    crane_type = 'normal crane'
                elif 'floating' in t:
                    crane_type = 'normal crane'  # floating crane 不直接作为产线吊机类型，归入 normal
                elif 'gantry' in t:
                    crane_type = 'gantry crane'

            for _ in range(qty):
                cranes.append({'capacity': capacity, 'type': crane_type})

    return cranes


def _calculate_line_size(line):
    """估算产线大小，用于排序。越大返回越大。"""
    capacity = line.get('capacity')
    if isinstance(capacity, (int, float)) and capacity > 0:
        return capacity

    dim = line.get('dimensions')
    if dim:
        nums = _extract_dimension_numbers(dim)
        if nums:
            # 用长×宽×深估算
            prod = 1
            for n in nums[:3]:
                prod *= n
            return prod

    return 0


def _extract_dimension_numbers(dim_str):
    """从 dimensions 字符串中提取数字列表。"""
    if not dim_str:
        return []
    s = str(dim_str).replace(' ', '').replace('��', '×').replace('x', '×').replace('X', '×').replace('*', '×')
    parts = s.split('×')
    nums = []
    for p in parts:
        p = re.sub(r'[^\d\.]', '', p)
        try:
            nums.append(float(p))
        except ValueError:
            pass
    return nums


def _assign_cranes_to_lines(lines, cranes):
    """
    将吊机分配到各产线。
    策略：按产线大小从大到小排序，大产线优先获得更多、更大的吊机。
    返回新的 production_lines（保留原顺序）。
    """
    if not isinstance(lines, list) or not lines:
        return lines

    # 备份原顺序
    original_order = [line.get('no') for line in lines]

    # 按大小降序排序产线
    sorted_lines = sorted(enumerate(lines), key=lambda x: _calculate_line_size(x[1]), reverse=True)

    # 按容量降序排序吊机
    sorted_cranes = sorted(cranes, key=lambda c: c.get('capacity', 0), reverse=True)

    total_lines = len(lines)
    total_cranes = len(sorted_cranes)

    if total_lines == 0 or total_cranes == 0:
        # 无吊机，清空 cranes
        for line in lines:
            line['cranes'] = []
        return lines

    # 先尽量平均分配基数
    base = total_cranes // total_lines
    extra = total_cranes % total_lines

    # 每个产线分配的吊机数量：大产线多一个
    assignments = [base] * total_lines
    for i in range(extra):
        assignments[i] += 1

    # 按排序后的产线顺序分配
    new_lines = []
    crane_idx = 0
    for rank, (orig_idx, line) in enumerate(sorted_lines):
        assigned_count = assignments[rank]
        assigned = sorted_cranes[crane_idx:crane_idx + assigned_count]
        crane_idx += assigned_count

        new_line = dict(line)
        new_line['cranes'] = [
            {'capacity': c.get('capacity'), 'type': c.get('type', 'gantry crane')}
            for c in assigned
        ]
        new_lines.append((orig_idx, new_line))

    # 恢复原始顺序
    new_lines.sort(key=lambda x: x[0])

    # 重新生成全局连续 crane no
    global_no = 1
    for _, line in new_lines:
        for crane in line.get('cranes', []):
            crane['no'] = global_no
            global_no += 1

    return [line for _, line in new_lines]


def _regenerate_production_lines(shipyard_name, introduce_text, current_lines, crane_parameters):
    """
    当 production_lines 格式校验失败或吊机不匹配时，调用 AI 重新生成。
    最多重试 2 次，仍失败返回原值。
    """
    current_lines_json = json.dumps(current_lines, ensure_ascii=False, indent=2) if current_lines else "null"

    prompt = f"""
The previously extracted production_lines for shipyard "{shipyard_name}" has format errors or crane mismatches.
Please re-extract production_lines from the shipyard introduction text below and return STRICTLY in the following JSON format.

CRITICAL STRUCTURE FOR production_lines - FOLLOW EXACTLY:
Each production line MUST contain these fields:
- no: integer, line number (1, 2, 3...)
- type: string, MUST be exactly ONE of ["slipway", "dry dock", "land assembly platform"]
- capacity: number ONLY, no unit (ton). Use null if unknown.
- dimensions: string with numbers separated by " × ", no units (meter). Use null if unknown.
- primary_function: string, main use/purpose of this line (e.g., "VLCC construction", "container ship", "ship repair"). Use English. Use null if unknown.
- is_active: string, MUST be exactly "Y" or "N". Y = active, N = closed/dismantled. Default "Y".
- cranes: ARRAY of crane objects. EACH production line can have MULTIPLE cranes.
  Each crane object contains:
  - no: integer, crane number (globally unique across all cranes, starting from 1 and continuous)
  - type: string, MUST be exactly ONE of ["gantry crane", "normal crane"]
  - capacity: number ONLY, no unit (ton). Use null if unknown.

CRANE ASSIGNMENT RULES:
1. Total number of cranes across all production_lines MUST equal the total individual cranes parsed from crane_parameters: "{crane_parameters or 'N/A'}".
2. If the text explicitly mentions which dock/shipway has which cranes, follow the text.
3. Otherwise, assign MORE and STRONGER cranes to LARGER production lines (compare dimensions first, then capacity).

ANTI-HALLUCINATION RULES - CRITICAL:
- ALL numeric values MUST be explicitly found in the source text.
- DO NOT copy numbers from the example below.
- If dimensions are not mentioned, set dimensions to null.
- If individual crane capacities are not mentioned, set cranes[].capacity to null.
- If the text only gives a TOTAL lifting capacity (e.g., "shipway max lifting capacity is 700 tons") without breaking it down, put ONE crane with that capacity or leave cranes empty.
- ALL text values MUST be in English ONLY. Do NOT return Chinese.

Return ONLY valid JSON (no markdown, no explanation):
{{
    "production_lines": [
        {{
            "no": 1,
            "type": "dry dock",
            "capacity": [value from text, or null],
            "dimensions": "[length] × [width] × [depth]" (or null),
            "primary_function": [value from text, or null],
            "is_active": "Y",
            "cranes": [
                {{"no": 1, "type": "gantry crane", "capacity": [value from text, or null]}}
            ]
        }}
    ]
}}

Current problematic production_lines (for reference only):
{current_lines_json}
"""

    for attempt in range(2):
        result_str = call_qwen_api(prompt, introduce_text)
        if not result_str:
            continue

        try:
            result_str = result_str.strip()
            if result_str.startswith('```json'):
                result_str = result_str[7:]
            if result_str.startswith('```'):
                result_str = result_str[3:]
            if result_str.endswith('```'):
                result_str = result_str[:-3]
            result_str = result_str.strip()

            result = json.loads(result_str)
            new_lines = result.get('production_lines')
            if isinstance(new_lines, list):
                new_lines = _normalize_production_lines(new_lines)
                is_valid, err = _strict_validate_production_lines(new_lines)
                if is_valid:
                    print(f"✓ production_lines 重生成成功（尝试 {attempt + 1}）")
                    return new_lines
                else:
                    print(f"⚠️ 重生成后格式仍无效: {err}")
        except Exception as e:
            print(f"⚠️ 重生成解析失败: {e}")

    print("⚠️ production_lines 重生成失败，保留原值")
    return current_lines


def _validate_and_fix_production_lines(all_info, introduce_text):
    """
    对提取结果中的 production_lines 进行最终校验和修正。
    包括：规范化、严格校验、吊机数量匹配、必要时重生成。

    吊机分配策略（始终生效）：
      - 从 crane_parameters 解析出所有独立吊机。
      - 按产线大小（capacity/dimensions）从大到小排序。
      - 按吊机起重能力从大到小排序。
      - 尽量平均分配到各产线；不能整除时，大产线优先多分得一台。
      - 分配时按排序依次把大吊机配给大产线，实现“大吊机优先到大船坞”。
    """
    lines = all_info.get('production_lines')
    if not lines:
        return

    # 1. 规范化
    lines = _normalize_production_lines(lines)

    # 2. 严格校验
    is_valid, err = _strict_validate_production_lines(lines)
    crane_params = all_info.get('crane_parameters', '')
    parsed_cranes = _parse_crane_parameters(crane_params)

    # 3. 吊机数量匹配检查
    crane_count_in_lines = sum(len(line.get('cranes', [])) for line in lines)
    crane_mismatch = False
    if parsed_cranes and crane_count_in_lines != len(parsed_cranes):
        crane_mismatch = True
        print(f"⚠️ 吊机数量不匹配: production_lines 中有 {crane_count_in_lines} 台，crane_parameters 解析出 {len(parsed_cranes)} 台")

    # 4. 始终使用智能分配重新分配吊机（大吊机优先分配到大船坞）
    if parsed_cranes:
        print(f"  根据 crane_parameters 重新分配吊机：共 {len(parsed_cranes)} 台，大吊机优先分配到大产线/大船坞...")
        reassigned = _assign_cranes_to_lines(lines, parsed_cranes)
        reassigned = _normalize_production_lines(reassigned)
        is_valid2, err2 = _strict_validate_production_lines(reassigned)
        crane_count2 = sum(len(line.get('cranes', [])) for line in reassigned)
        if is_valid2 and crane_count2 == len(parsed_cranes):
            print(f"✓ 吊机智能分配成功，共 {crane_count2} 台吊机")
            all_info['production_lines'] = reassigned
            return
        else:
            print(f"⚠️ 智能分配后仍有问题: {err2 or '吊机数量不匹配'}，继续后续处理...")

    # 5. 如果校验失败或数量不匹配，调用 AI 重生成
    if not is_valid or crane_mismatch:
        shipyard_name = all_info.get('English_name', all_info.get('shipyard_name', 'Unknown'))
        if not is_valid:
            print(f"⚠️ production_lines 格式校验失败: {err}，尝试重生成...")
        else:
            print("⚠️ production_lines 吊机数量不匹配，尝试重生成...")

        new_lines = _regenerate_production_lines(shipyard_name, introduce_text, lines, crane_params)
        new_lines = _normalize_production_lines(new_lines)
        is_valid3, err3 = _strict_validate_production_lines(new_lines)
        if is_valid3:
            all_info['production_lines'] = new_lines
            print("✓ production_lines 已通过最终校验")
        else:
            print(f"⚠️ production_lines 最终校验仍失败: {err3}，保留原值")
            all_info['production_lines'] = lines
    else:
        all_info['production_lines'] = lines
        print(f"✓ production_lines 通过校验，共 {crane_count_in_lines} 台吊机")


def extract_production_facilities(introduce_text: str) -> Dict[str, Any]:
    """
    Extract production line parameters and supporting facilities
    """
    prompt = prompt_builder.build_extraction_prompt('facilities', introduce_text)

    result_str = call_qwen_api(prompt, introduce_text)
    if result_str:
        result = _parse_llm_json(result_str)
        if not result:
            print('JSON parsing failed for production_facilities')
            return {}
        # 规范化单位
        if 'production_lines' in result:
            result['production_lines'] = _normalize_production_lines(result['production_lines'])
        if 'crane_parameters' in result:
            result['crane_parameters'] = _normalize_crane_parameters(result['crane_parameters'])
        return result
    return {}


def extract_core_capabilities(introduce_text: str) -> Dict[str, Any]:
    """
    Extract core capabilities and main ship types (DEPRECATED - no longer used)
    Kept for backward compatibility but returns empty dict.
    """
    return {}


def extract_all_info(shipyard_name: str, introduce_text: str, default_names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Extract all structured information of the shipyard.

    Args:
        shipyard_name: 船厂名称（已标准化）
        introduce_text: 用于提取的船厂介绍/新闻正文
        default_names: 可选，用于在 AI 未提取到名称时作为默认值的字典，
                       例如 {"English_name": "...", "Local_name": "..."}。
                       在 GUI 等无交互环境中使用，避免 input() 阻塞。
    """
    print(f"\n{'='*80}")
    print(f"Processing shipyard: {shipyard_name}")
    print(f"{'='*80}")

    all_info: Dict[str, Any] = {}
    default_names = default_names or {}

    # 1. Extract basic information (including former_name)
    print("Extracting basic information...")
    basic_info = extract_basic_info(introduce_text)
    all_info.update(basic_info)

    # Validate and ensure English_name and Local_name are not null
    print("\n[验证] 检查船厂名称信息...")

    def _coerce_name(value):
        if isinstance(value, str):
            s = value.strip()
            if s and s.lower() != 'null':
                return s
        return None

    english_name = _coerce_name(all_info.get('English_name'))
    local_name = _coerce_name(all_info.get('Local_name'))

    # 使用默认值补全缺失的名称
    if not english_name and default_names.get('English_name'):
        english_name = _coerce_name(default_names['English_name'])
        if english_name:
            all_info['English_name'] = english_name
            print(f"✓ 使用默认值 English_name: {english_name}")

    if not local_name and default_names.get('Local_name'):
        local_name = _coerce_name(default_names['Local_name'])
        if local_name:
            all_info['Local_name'] = local_name
            print(f"✓ 使用默认值 Local_name: {local_name}")

    # 只要 English_name / Local_name 至少有一个即可；两者都缺失才需要兜底/交互
    if english_name or local_name:
        if english_name:
            print(f"✓ English_name: {english_name}")
        if local_name:
            print(f"✓ Local_name: {local_name}")
    else:
        # Always use non-interactive fallback (no GUI/CLI input in FastAPI context)
        fallback = shipyard_name or "Unknown Shipyard"
        all_info['English_name'] = fallback
        all_info['Local_name'] = fallback
        print(f"⚠️  AI未能提取到 English_name 和 Local_name，使用船厂名兜底: {fallback}")

    # 2. Extract qualifications and capacity
    print("Extracting qualifications and capacity...")
    qualifications = extract_qualifications_and_capacity(introduce_text)
    all_info.update(qualifications)

    # 3. Extract shipyard scale
    print("Extracting shipyard scale...")
    scale = extract_shipyard_scale(introduce_text)
    all_info.update(scale)

    # 4. Extract production facilities
    print("Extracting production facilities...")
    facilities = extract_production_facilities(introduce_text)
    all_info.update(facilities)

    # 4.5 Validate and fix production_lines
    if all_info.get('production_lines'):
        print("\n[验证] 检查 production_lines 格式与吊机匹配...")
        _validate_and_fix_production_lines(all_info, introduce_text)

    # 5. 统一字段格式校验与重生成流水线（跳过已由专门逻辑处理的复杂字段）
    print("\n[验证] 运行统一字段格式校验与重生成...")
    all_info = validation.run_validation_pipeline(
        introduce_text,
        all_info,
        _llm_call,
        skip_fields=['production_lines', 'annual_production_capacity']
    )

    # 6. Core capabilities extraction REMOVED (main_ship_types_now and ship_types_delivered no longer generated)

    # 6. Auto-generate Abreviation if missing (no DB lookup in this standalone module)
    abbr = all_info.get('Abreviation')
    if not abbr or str(abbr).strip().lower() in ('', 'null', 'none'):
        print("[Abreviation] AI未提供缩写，正在自动生成...")
        english_name = all_info.get('English_name', shipyard_name)
        generated = generate_abbreviation(english_name, set())
        if generated:
            all_info['Abreviation'] = generated
            print(f"[Abreviation] 已自动生成: {generated}")
        else:
            print("[Abreviation] 自动生成失败")

    print(f"✓ Completed information extraction for shipyard {shipyard_name}")
    return all_info


def generate_abbreviation(english_name: str, existing_abbrs: set) -> Optional[str]:
    """
    基于英文船厂名称自动生成缩写
    规则：
    1. 取每个主要单词的首字母（大写），去掉通用后缀
    2. 长度控制在 2-5 个字符
    3. 确保不与现有缩写重复，重复则加数字后缀
    """
    if not english_name:
        return None

    name = english_name.strip()

    # 通用后缀/停用词
    stop_words = {
        'co', 'ltd', 'limited', 'inc', 'corp', 'corporation', 'company',
        'group', 'holdings', 'holding', 'and', 'of', 'the', 's', 'llc',
        'shipbuilding', 'shipyard', 'marine', 'heavy', 'industry',
        'industries', 'engineering', 'offshore', 'equipment', 'factory',
        'works', 'yard', 'base', 'port'
    }

    # 提取单词（使用 [A-Za-z]+ 以正确匹配全大写缩写如 ZPMC）
    words = re.findall(r"[A-Za-z]+", name)
    content_words = [w for w in words if w.lower() not in stop_words]

    candidates = []

    # 候选1: 首字母组合
    if content_words:
        abbr = ''.join(w[0].upper() for w in content_words)
        candidates.append(abbr)

    # 候选2: 如果首字母太长，取前4个字母
    if candidates and len(candidates[0]) > 5:
        candidates.append(candidates[0][:4])

    # 候选3: 如果首字母太短，用前3-4个大写字母
    if candidates and len(candidates[0]) < 2:
        all_upper = re.sub(r'[^A-Za-z]', '', name)
        candidates.append(all_upper[:4].upper())

    # 候选4: 纯大写字母组合（如 ZPMC）
    all_upper = re.sub(r'[^A-Z]', '', name)
    if len(all_upper) >= 2:
        candidates.append(all_upper[:5])

    # 候选5: 前几个字母
    clean = re.sub(r'[^A-Za-z]', '', name)
    if len(clean) >= 2:
        candidates.append(clean[:4].upper())

    # 去重并选择最合适的
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        abbr = candidate
        if len(abbr) > 5:
            abbr = abbr[:5]
        if len(abbr) < 2:
            continue

        # 检查是否重复
        if abbr not in existing_abbrs:
            return abbr

        # 尝试加数字后缀
        for i in range(1, 100):
            new_abbr = f"{abbr}{i}"
            if new_abbr not in existing_abbrs:
                return new_abbr

    # 兜底：随机生成
    for _ in range(100):
        abbr = ''.join(random.choices(string.ascii_uppercase, k=3))
        if abbr not in existing_abbrs:
            return abbr

    return None



if __name__ == "__main__":
    import sys

    sample_text = ""
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            sample_text = f.read()
    if not sample_text:
        sample_text = "Sample shipyard description."

    result = extract_all_info("Sample Shipyard", sample_text)
    print(json.dumps(result, ensure_ascii=False, indent=2))

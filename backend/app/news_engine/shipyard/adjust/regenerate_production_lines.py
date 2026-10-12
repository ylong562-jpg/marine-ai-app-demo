"""
产线数据再生成脚本 (v2 - cranes 数组版)
========================
功能：读取现有数据库中的 production_lines 和 crane_parameters，
      使用 AI 重新生成 production_lines（包含 type 和 cranes 数组），
      覆盖旧数据，保留 crane_parameters 不变。

关键修复：
- crane_parameters 中的每台吊机都必须被正确分配到 production_lines 的 cranes 数组中
- 例如 "2×900t gantry;4×300t gantry" = 6 台吊机，必须分配到产线的 cranes 数组中

运行方式：
    python regenerate_production_lines.py
    python regenerate_production_lines.py --limit 5
    python regenerate_production_lines.py --shipyard-id 123
    python regenerate_production_lines.py --force
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import time
import argparse
from typing import Dict, Any, List, Optional, Tuple

import mysql.connector
from mysql.connector import Error

try:
    from ai_extract_shipyard_info import call_qwen_api, DB_CONFIG
except ImportError:
    import dashscope
    from http import HTTPStatus
    from config import DB_CONFIG
    dashscope.api_key = os.getenv('DASHSCOPE_API_KEY')
    def call_qwen_api(prompt: str, text: str, model: str = 'qwen-plus') -> Optional[str]:
        try:
            messages = [
                {'role': 'system', 'content': 'You are a professional maritime industry data analyst.'},
                {'role': 'user', 'content': f"{prompt}\n\n{text}"}
            ]
            response = dashscope.Generation.call(
                model=model, messages=messages, result_format='message',
                temperature=0.1, top_p=0.8
            )
            if response.status_code == HTTPStatus.OK:
                return response['output']['choices'][0]['message']['content']
            print(f'API call failed: {response.code} - {response.message}')
            return None
        except Exception as e:
            print(f'API call exception: {e}')
            return None


VALID_LINE_TYPES = {'slipway', 'dry dock'}
VALID_CRANE_TYPES = {'gantry crane', 'normal crane'}
MAX_RETRY = 3


def connect_db():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        print("[DB] 数据库连接成功")
        return conn
    except Error as e:
        print(f"[DB] 数据库连接失败: {e}")
        return None


def fetch_shipyards_to_process(conn, limit=None, shipyard_id=None):
    cursor = conn.cursor(dictionary=True)
    if shipyard_id:
        sql = "SELECT id, English_name, Local_name, production_lines, crane_parameters, introduce FROM shipyard_data WHERE id = %s"
        cursor.execute(sql, (shipyard_id,))
    elif limit:
        sql = "SELECT id, English_name, Local_name, production_lines, crane_parameters, introduce FROM shipyard_data WHERE ai_processed = TRUE AND production_lines IS NOT NULL LIMIT %s"
        cursor.execute(sql, (limit,))
    else:
        sql = "SELECT id, English_name, Local_name, production_lines, crane_parameters, introduce FROM shipyard_data WHERE ai_processed = TRUE AND production_lines IS NOT NULL"
        cursor.execute(sql)
    results = cursor.fetchall()
    cursor.close()
    return results


def update_production_lines(conn, shipyard_id: int, new_lines: List[Dict]):
    cursor = conn.cursor()
    sql = "UPDATE shipyard_data SET production_lines = %s WHERE id = %s"
    cursor.execute(sql, (json.dumps(new_lines, ensure_ascii=False), shipyard_id))
    conn.commit()
    cursor.close()


def _parse_existing_lines(value) -> List[Dict]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    if isinstance(value, list):
        return value
    return []


def _parse_crane_parameters(crane_str: str) -> List[Dict]:
    """
    将 crane_parameters 字符串解析为单台吊机列表。
    例如: "2×900t gantry cranes;4×300t gantry cranes"
    返回: [{"no":1,"capacity":900,"type":"gantry crane"}, {"no":2,"capacity":900,...}, ...]
    共 6 台吊机。
    """
    if not crane_str or not isinstance(crane_str, str):
        return []

    individual_cranes = []
    segments = [s.strip() for s in crane_str.split(';') if s.strip()]

    for seg in segments:
        # 模式1: 数量×吨位 类型，如 "2×900t gantry cranes"
        m = re.match(r'^(\d+)\s*[xX×*]\s*([\d,\.]+)\s*(?:t|tons?|tonnes?)\s+(.+)$', seg, re.IGNORECASE)
        if m:
            count = int(m.group(1))
            cap_str = m.group(2).replace(',', '')
            try:
                cap = float(cap_str)
                cap = int(cap) if cap == int(cap) else cap
            except ValueError:
                cap = 0
            ctype_raw = m.group(3).strip().lower()
            ctype = 'gantry crane' if ('gantry' in ctype_raw or '龙门' in ctype_raw or '门式' in ctype_raw) else 'normal crane'
            for _ in range(count):
                individual_cranes.append({'capacity': cap, 'type': ctype})
            continue

        # 模式2: 匹配 "数量×吨位" 格式（必须有 ×/x/* 分隔符），如 "2×900t"
        # 注意：不能把 "600T" 误解析为 "60 × 0t"，因此 × 分隔符是强制的
        found = re.findall(r'(\d+)\s*[xX×*]\s*([\d,\.]+)\s*(?:t|tons?|T)\b', seg, re.IGNORECASE)
        mode2_count_before = len(individual_cranes)
        if found:
            for count_str, cap_str in found:
                count = int(count_str) if count_str else 1
                cap_str = cap_str.replace(',', '')
                try:
                    cap = float(cap_str)
                    cap = int(cap) if cap == int(cap) else cap
                except ValueError:
                    cap = 0
                # sanity check: 数量异常大或吨位为0时，跳过模式2，让模式3处理
                if count > 50 or cap == 0:
                    continue
                ctype_raw = seg.lower()
                ctype = 'gantry crane' if ('gantry' in ctype_raw or '龙门' in ctype_raw or '门式' in ctype_raw) else 'normal crane'
                for _ in range(count):
                    individual_cranes.append({'capacity': cap, 'type': ctype})
            # 如果模式2产生了有效吊机，则跳过后续模式；否则继续尝试模式3
            if len(individual_cranes) > mode2_count_before:
                continue

        # 模式3: 提取 "数字t" 或 "数字T" 的所有出现，如 "600T+600T+1000T"
        items = re.findall(r'([\d,\.]+)\s*(?:t|tons?|T)\b', seg, re.IGNORECASE)
        if items:
            for cap_str in items:
                cap_str = cap_str.replace(',', '')
                try:
                    cap = float(cap_str)
                    cap = int(cap) if cap == int(cap) else cap
                except ValueError:
                    cap = 0
                ctype_raw = seg.lower()
                ctype = 'gantry crane' if ('gantry' in ctype_raw or '龙门' in ctype_raw or '门式' in ctype_raw) else 'normal crane'
                individual_cranes.append({'capacity': cap, 'type': ctype})
            continue

        # 模式4: 最后尝试 "数量 类型 吨位" 如 "1台500T"
        m3 = re.search(r'(\d+)\s*(?:台|套|架|座)?\s*([\d,\.]+)\s*(?:t|tons?|T)', seg, re.IGNORECASE)
        if m3:
            cap_str = m3.group(2).replace(',', '')
            try:
                cap = float(cap_str)
                cap = int(cap) if cap == int(cap) else cap
            except ValueError:
                cap = 0
            ctype_raw = seg.lower()
            ctype = 'gantry crane' if ('gantry' in ctype_raw or '龙门' in ctype_raw or '门式' in ctype_raw) else 'normal crane'
            individual_cranes.append({'capacity': cap, 'type': ctype})

    # 去重：如果完全相同的 capacity 和 type 连续出现多次，可能是解析错误
    # 但对于 "2×900t" 来说，确实应该有 2 台相同的
    return individual_cranes


def _extract_dimensions_value(dim_str) -> float:
    if not dim_str:
        return 0.0
    s = str(dim_str)
    nums = re.findall(r'[\d\.]+', s.replace(',', ''))
    try:
        values = [float(n) for n in nums]
        if not values:
            return 0.0
        prod = 1.0
        for v in values:
            prod *= v
        return prod
    except ValueError:
        return 0.0


def _line_size_key(line: Dict) -> float:
    """用于排序产线大小：有 dimensions 用 dimensions 乘积，否则用 capacity"""
    dim_val = _extract_dimensions_value(line.get('dimensions'))
    if dim_val > 0:
        return dim_val
    cap = line.get('capacity')
    if cap is not None:
        try:
            return float(cap)
        except (ValueError, TypeError):
            pass
    return 0.0


def _is_already_new_format(lines: List[Dict]) -> bool:
    if not lines:
        return True
    for line in lines:
        if isinstance(line, dict):
            if 'cranes' in line and isinstance(line.get('cranes'), list) and line['cranes']:
                return True
    return False


def _extract_pure_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
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
    if not dim_str:
        return None
    s = str(dim_str).strip()
    s = re.sub(r'\s*(?:meters?|metres?|m|ft|feet|yards?|yd)\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*[xX×*]\s*', ' × ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s if s else None


def validate_lines_strict(lines: List[Dict], expected_crane_count: int = None) -> Tuple[List[Dict], bool]:
    if not isinstance(lines, list):
        return [], False

    all_valid = True
    fixed_lines = []
    errors = []
    total_cranes = 0

    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            all_valid = False
            errors.append(f"  产线[{idx}] 不是字典类型")
            continue

        fixed = dict(line)

        line_type = str(fixed.get('type', '')).strip().lower()
        if line_type not in VALID_LINE_TYPES:
            all_valid = False
            errors.append(f"  产线[{idx}] type='{line_type}' 不合法")
            continue

        # 验证 cranes 数组
        cranes = fixed.get('cranes')
        if isinstance(cranes, list):
            for cidx, crane in enumerate(cranes):
                if isinstance(crane, dict):
                    crane_type = str(crane.get('type', '')).strip().lower()
                    if crane_type not in VALID_CRANE_TYPES:
                        all_valid = False
                        errors.append(f"  产线[{idx}] cranes[{cidx}].type='{crane_type}' 不合法")
                        continue
                    c_cap = crane.get('capacity')
                    if c_cap is not None:
                        pure = _extract_pure_number(c_cap)
                        if pure is not None:
                            crane['capacity'] = pure
                    try:
                        crane['no'] = int(crane['no'])
                    except (ValueError, TypeError):
                        crane['no'] = cidx + 1
                else:
                    all_valid = False
                    errors.append(f"  产线[{idx}] cranes[{cidx}] 不是字典")
                    continue
            total_cranes += len(cranes)
        elif 'crane' in fixed:
            # 兼容旧格式
            old_crane = fixed.pop('crane')
            if isinstance(old_crane, dict):
                c_cap = old_crane.get('capacity')
                if c_cap is not None:
                    pure = _extract_pure_number(c_cap)
                    if pure is not None:
                        old_crane['capacity'] = pure
                fixed['cranes'] = [old_crane]
                total_cranes += 1
            else:
                fixed['cranes'] = []
        else:
            fixed['cranes'] = []

        cap = fixed.get('capacity')
        if cap is not None:
            pure = _extract_pure_number(cap)
            if pure is not None:
                fixed['capacity'] = pure

        dim = fixed.get('dimensions')
        if dim is not None:
            normalized = _normalize_dimensions(dim)
            if normalized is not None:
                fixed['dimensions'] = normalized

        try:
            fixed['no'] = int(fixed.get('no', 1))
        except (ValueError, TypeError):
            fixed['no'] = 1

        fixed_lines.append(fixed)

    # 检查吊机总数
    if expected_crane_count is not None and total_cranes != expected_crane_count:
        all_valid = False
        errors.append(f"  吊机总数不匹配: AI返回{total_cranes}台, 期望{expected_crane_count}台")

    if errors:
        print("  [验证失败]")
        for e in errors:
            print(e)

    return fixed_lines, all_valid


def assign_cranes_to_lines(lines: List[Dict], individual_cranes: List[Dict]) -> List[Dict]:
    """
    将单台吊机列表按规则分配到各产线的 cranes 数组中。
    规则：
    1. 按产线大小排序（dimensions 乘积 > capacity）
    2. 按吊机 capacity 排序（从大到小）
    3. 数量尽可能均分，无法均分时大产线多分、多分强的吊机
    """
    if not individual_cranes or not lines:
        for line in lines:
            line['cranes'] = []
        return lines

    n_lines = len(lines)
    n_cranes = len(individual_cranes)

    # 按产线大小排序（从大到小）
    line_indices = sorted(range(n_lines), key=lambda i: _line_size_key(lines[i]), reverse=True)

    # 按吊机 capacity 排序（从大到小）
    sorted_cranes = sorted(enumerate(individual_cranes), key=lambda x: x[1]['capacity'], reverse=True)

    # 计算每条产线分配多少台
    base = n_cranes // n_lines
    remainder = n_cranes % n_lines
    counts = [base] * n_lines
    for i in range(remainder):
        counts[line_indices[i]] += 1

    # 分配吊机：强的吊机优先给大的产线
    crane_ptr = 0
    assignments = [[] for _ in range(n_lines)]
    for idx in line_indices:
        need = counts[idx]
        for _ in range(need):
            if crane_ptr < n_cranes:
                _, crane = sorted_cranes[crane_ptr]
                assignments[idx].append(dict(crane))
                crane_ptr += 1

    # 写入 lines 并重新编号
    global_crane_no = 1
    for idx in range(n_lines):
        lines[idx]['cranes'] = assignments[idx]
        for c in lines[idx]['cranes']:
            c['no'] = global_crane_no
            global_crane_no += 1

    return lines


def build_prompt(existing_lines: List[Dict], crane_params: str, introduce: str, individual_cranes: List[Dict]) -> str:
    lines_json = json.dumps(existing_lines, ensure_ascii=False, indent=2)
    cranes_list = [{"no": i+1, "capacity": c["capacity"], "type": c["type"]} for i, c in enumerate(individual_cranes)]
    cranes_json = json.dumps(cranes_list, ensure_ascii=False, indent=2)

    prompt = f"""You are a professional maritime industry data analyst. Your task is to regenerate the production_lines data for a shipyard.

## EXISTING DATA

### Current production_lines:
```json
{lines_json}
```

### Current crane_parameters (raw string):
{crane_params if crane_params else 'Not available'}

### INDIVIDUAL CRANES (already parsed for you, TOTAL {len(individual_cranes)} cranes):
```json
{cranes_json}
```

### Shipyard introduction (for context):
{introduce if introduce else 'Not available'}

## REGENERATION RULES — FOLLOW STRICTLY

### 1. Output Structure
Each production line MUST be an object with exactly these fields:
- `no`: integer, line number
- `type`: string, MUST be exactly ONE of ["slipway", "dry dock"]
- `capacity`: number ONLY, no unit. Default unit is ton.
- `dimensions`: string with numbers separated by " × ", no units. Default unit is meter.
- `cranes`: ARRAY of crane objects. The total number of cranes across ALL production_lines MUST be exactly {len(individual_cranes)}. Each crane object contains:
  - `no`: integer, globally unique crane number (1 to {len(individual_cranes)})
  - `type`: string, MUST be exactly ONE of ["gantry crane", "normal crane"]
  - `capacity`: number ONLY, no unit. Default unit is t.

### 2. Type Classification Rules
- If dimensions has 3 parts (length × width × depth) → "dry dock"
- If dimensions has 2 parts (length × width) → "slipway"
- If no dimensions but capacity > 50000 → "dry dock"
- If no dimensions and capacity <= 50000 → "slipway"

### 3. Crane Assignment Rules
Total individual cranes: {len(individual_cranes)}. They must be distributed across {len(existing_lines)} production_lines.
- Sort production_lines by size (larger first): compare dimensions product (L×W×D or L×W). If dimensions unavailable, compare capacity.
- Sort cranes by capacity (stronger first).
- Assign MORE cranes and STRONGER cranes to LARGER production_lines.
- Example: 2 lines, 5 cranes [900, 900, 600, 300, 300]. Line1 (larger) gets 3 cranes [900, 900, 600]. Line2 (smaller) gets 2 cranes [300, 300].

### 4. Number of Output Lines
- The number of production_lines in output MUST equal {len(existing_lines)}.

### 5. Unit Rules
- capacity: pure number, unit is ton by default.
- dimensions: pure numbers separated by " × ", unit is meter by default.
- cranes[].capacity: pure number, unit is t by default.

## RETURN FORMAT
Return ONLY a valid JSON array. No markdown, no explanation, no code blocks.

Example output (if there were 3 lines and 5 cranes):
[
  {{
    "no": 1,
    "type": "dry dock",
    "capacity": 30000,
    "dimensions": "530 × 130 × 13.8",
    "cranes": [
      {{"no": 1, "type": "gantry crane", "capacity": 900}},
      {{"no": 2, "type": "gantry crane", "capacity": 900}},
      {{"no": 3, "type": "gantry crane", "capacity": 600}}
    ]
  }},
  {{
    "no": 2,
    "type": "slipway",
    "capacity": 15000,
    "dimensions": "310 × 68",
    "cranes": [
      {{"no": 4, "type": "normal crane", "capacity": 300}},
      {{"no": 5, "type": "normal crane", "capacity": 300}}
    ]
  }}
]
"""
    return prompt


def call_ai_for_regeneration(existing_lines: List[Dict], crane_params: str, introduce: str, individual_cranes: List[Dict]) -> Optional[List[Dict]]:
    prompt = build_prompt(existing_lines, crane_params, introduce, individual_cranes)
    result_str = call_qwen_api(prompt, "Regenerate production_lines with cranes array.")
    if not result_str:
        return None
    result_str = result_str.strip()
    if result_str.startswith('```json'):
        result_str = result_str[7:]
    if result_str.startswith('```'):
        result_str = result_str[3:]
    if result_str.endswith('```'):
        result_str = result_str[:-3]
    result_str = result_str.strip()
    try:
        return json.loads(result_str)
    except json.JSONDecodeError as e:
        print(f"  [AI] JSON 解析失败: {e}")
        print(f"  [AI] 原始返回: {result_str[:500]}")
        return None


def process_shipyard(conn, shipyard: Dict, force: bool = False) -> bool:
    sid = shipyard['id']
    name = shipyard.get('Local_name') or shipyard.get('English_name') or f'ID:{sid}'

    print(f"\n{'='*60}")
    print(f"处理船厂: {name} (ID: {sid})")
    print(f"{'='*60}")

    existing_lines = _parse_existing_lines(shipyard.get('production_lines'))
    crane_params = shipyard.get('crane_parameters', '') or ''
    introduce = shipyard.get('introduce', '') or ''

    if not existing_lines:
        print(f"  [跳过] 没有现有 production_lines 数据")
        return False

    # 解析 crane_parameters 为单台吊机列表
    individual_cranes = _parse_crane_parameters(crane_params)
    print(f"  现有产线数: {len(existing_lines)}")
    print(f"  吊机参数: {crane_params}")
    print(f"  解析出单台吊机: {len(individual_cranes)} 台")
    if individual_cranes:
        caps = [c['capacity'] for c in individual_cranes]
        print(f"  吊机吨位分布: {caps}")

    if not force and _is_already_new_format(existing_lines):
        # 再检查 cranes 总数是否匹配
        total_existing = sum(len(line.get('cranes', [])) for line in existing_lines if isinstance(line.get('cranes'), list))
        if total_existing == len(individual_cranes):
            print(f"  [跳过] 已经是新格式且吊机数量匹配 ({total_existing}台)")
            return False
        else:
            print(f"  [需更新] 已是新格式但吊机数量不匹配: 现有{total_existing}台, 应有{len(individual_cranes)}台")

    # 尝试多次调用 AI
    for attempt in range(1, MAX_RETRY + 1):
        print(f"  [AI] 第 {attempt} 次尝试生成...")
        new_lines = call_ai_for_regeneration(existing_lines, crane_params, introduce, individual_cranes)

        if new_lines is None:
            print(f"  [AI] 返回为空，准备重试...")
            time.sleep(1)
            continue

        # 严格验证
        fixed_lines, all_valid = validate_lines_strict(new_lines, expected_crane_count=len(individual_cranes))

        if all_valid:
            print(f"  [验证] 全部通过，吊机数量: {len(individual_cranes)}台")
        else:
            print(f"  [验证] AI 返回不符合要求，使用脚本自动分配吊机...")
            # 脚本兜底：保留 AI 生成的产线结构，但自己重新分配吊机
            fixed_lines = assign_cranes_to_lines(fixed_lines, individual_cranes)
            # 再次验证（只验证类型，不验证数量，因为脚本分配的肯定对）
            fixed_lines, _ = validate_lines_strict(fixed_lines, expected_crane_count=None)
            total = sum(len(line.get('cranes', [])) for line in fixed_lines)
            print(f"  [脚本分配] 完成，共分配 {total} 台吊机")

        # 检查产线数量
        if len(fixed_lines) != len(existing_lines):
            print(f"  [警告] 产线数量不匹配 ({len(fixed_lines)} vs {len(existing_lines)})，准备重试...")
            if attempt < MAX_RETRY:
                time.sleep(1)
                continue
            else:
                print(f"  [错误] 重试次数用尽，跳过")
                return False

        # 保存到数据库
        update_production_lines(conn, sid, fixed_lines)
        print(f"  [保存] production_lines 已更新")

        print(f"  结果预览:")
        for line in fixed_lines:
            crane_count = len(line.get('cranes', []))
            crane_caps = [c.get('capacity') for c in line.get('cranes', [])]
            print(f"    Line {line.get('no')} ({line.get('type')}): {crane_count}台吊机 {crane_caps}")

        return True

    print(f"  [失败] 所有重试均失败")
    return False


def main():
    parser = argparse.ArgumentParser(description='重新生成船厂 production_lines 数据')
    parser.add_argument('--limit', type=int, default=None, help='限制处理数量')
    parser.add_argument('--shipyard-id', type=int, default=None, help='只处理指定船厂ID')
    parser.add_argument('--force', action='store_true', help='强制重新处理')
    args = parser.parse_args()

    if not os.getenv('DASHSCOPE_API_KEY'):
        print("[错误] 未设置 DASHSCOPE_API_KEY 环境变量")
        return

    conn = connect_db()
    if not conn:
        return

    try:
        shipyards = fetch_shipyards_to_process(conn, limit=args.limit, shipyard_id=args.shipyard_id)
        total = len(shipyards)
        print(f"\n共找到 {total} 个需要处理的船厂")

        if total == 0:
            print("没有可处理的船厂")
            return

        success_count = 0
        skip_count = 0
        fail_count = 0

        for i, sy in enumerate(shipyards, 1):
            print(f"\n[{i}/{total}] ", end='')
            ok = process_shipyard(conn, sy, force=args.force)
            if ok:
                success_count += 1
            else:
                existing_lines = _parse_existing_lines(sy.get('production_lines'))
                individual_cranes = _parse_crane_parameters(sy.get('crane_parameters', ''))
                if not existing_lines:
                    skip_count += 1
                elif not args.force and _is_already_new_format(existing_lines):
                    total_existing = sum(len(line.get('cranes', [])) for line in existing_lines if isinstance(line.get('cranes'), list))
                    if total_existing == len(individual_cranes):
                        skip_count += 1
                    else:
                        fail_count += 1
                else:
                    fail_count += 1
            time.sleep(0.5)

        print(f"\n{'='*60}")
        print(f"处理完成: 成功={success_count}, 跳过={skip_count}, 失败={fail_count}")
        print(f"{'='*60}")

    finally:
        conn.close()
        print("[DB] 数据库连接已关闭")


if __name__ == '__main__':
    main()

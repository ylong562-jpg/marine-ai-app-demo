import mysql.connector
import json
import os
import re
import urllib.request
import urllib.parse
import time
from decimal import Decimal


class _DecimalEncoder(json.JSONEncoder):
    """Serialize Decimal values (from MySQL rows) as floats for JSON storage."""

    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)

DB_CONFIG = {
    'host': 'your-db-host.example.com',
    'user': 'your-db-user',
    'password': 'your-db-password',
    'database': 'Time_Series',
    'port': 3306
}


def geocode_photon(query):
    """使用 Photon (komoot) 免费地理编码服务获取坐标"""
    url = 'https://photon.komoot.io/api/?q=' + urllib.parse.quote(query) + '&limit=1'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if data.get('features') and len(data['features']) > 0:
                coords = data['features'][0]['geometry']['coordinates']
                return float(coords[1]), float(coords[0])  # lat, lng
    except Exception as e:
        pass
    return None


def get_precise_coords(name, location):
    """
    获取精确坐标：
    1. 先用 "船厂名 + 地址" 查询
    2. 失败则用 "地址" 查询
    3. 再失败则用预定义城市坐标
    """
    # 尝试1: 船厂名 + 地址
    query1 = f"{name}, {location}, China"
    result = geocode_photon(query1)
    if result:
        return result, 'precise'
    time.sleep(0.3)

    # 尝试2: 仅地址
    query2 = f"{location}, China"
    result = geocode_photon(query2)
    if result:
        return result, 'location'
    time.sleep(0.3)

    # 尝试3: 仅城市名（取逗号前第一个部分）
    city = location.split(',')[0].strip()
    query3 = f"{city}, China"
    result = geocode_photon(query3)
    if result:
        return result, 'city'
    time.sleep(0.3)

    return None, 'failed'


def normalize_production_lines(lines):
    """将旧格式单数 crane 转换为 cranes 数组，确保数据一致性，并补齐新字段"""
    if not isinstance(lines, list):
        return []
    for line in lines:
        if not isinstance(line, dict):
            continue
        if 'crane' in line and 'cranes' not in line:
            old_crane = line.pop('crane')
            if isinstance(old_crane, dict):
                line['cranes'] = [old_crane]
            else:
                line['cranes'] = []
        elif 'cranes' not in line:
            line['cranes'] = []

        # 补齐新字段
        if 'primary_function' not in line:
            line['primary_function'] = None
        else:
            pf = line['primary_function']
            if pf is not None:
                pf = str(pf).strip()
                line['primary_function'] = pf if pf else None

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

        # 规范化 dimensions 格式
        dim = line.get('dimensions')
        if dim is not None:
            s = str(dim).strip()
            if s:
                s = re.sub(r'[xX*]', '×', s)
                s = re.sub(r'\s*×\s*', ' × ', s)
                s = re.sub(r'\s+', ' ', s).strip()
                line['dimensions'] = s
    return lines


def load_existing_coords(json_path: str):
    """读取现有 shipyards_data.json，缓存已有坐标避免重复 geocode"""
    cache = {}
    if not os.path.exists(json_path):
        return cache
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for sy in data:
                if sy.get('lat') and sy.get('lng'):
                    cache[sy['id']] = {
                        'lat': sy['lat'],
                        'lng': sy['lng'],
                        'geo_level': sy.get('geo_level', 'precise')
                    }
    except Exception as e:
        print(f"[WARN] 读取现有坐标失败: {e}")
    return cache


def fetch_shipyards(output_path: str | None = None):
    """从数据库获取船厂数据并生成 JSON 文件。返回 (shipyards_list, summary_dict)。"""
    if output_path is None:
        # 默认输出到项目 backend/data/shipyards_data.json
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_path = os.path.join(script_dir, '..', '..', 'data', 'shipyards_data.json')
        output_path = os.path.abspath(output_path)

    print("[DB] 连接数据库...")
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        'SELECT id, Local_name, English_name, location, refund_guarantee_capacity, production_lines, '
        'annual_production_capacity, shipbuilding_transactions, newbuilding_transactions, update_time, update_person, '
        'is_verified, person_in_charge, is_active, shipyard_type, lat, lng, boundary_geo, '
        'employees, utilization_rate, orderbook_months, founded_year, '
        'former_name, state_owned, shipyard_area, Quayside, Block_Assembly_Workshop, painting_workshop, Outfitting_workshop, '
        'has_branch, branch_base '
        'FROM shipyard_data WHERE location IS NOT NULL AND location != %s',
        ('',)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    print(f"[DB] 获取到 {len(rows)} 个船厂")

    coord_cache = load_existing_coords(output_path)
    if coord_cache:
        print(f"[CACHE] 已缓存 {len(coord_cache)} 个已有坐标")

    shipyards = []
    failed = []
    geocoded = 0
    reused = 0

    for i, row in enumerate(rows, 1):
        name = row['Local_name'] or row['English_name'] or f'Shipyard #{row["id"]}'
        location = row['location']

        db_lat = row.get('lat')
        db_lng = row.get('lng')
        if db_lat is not None and db_lng is not None:
            coords = (float(db_lat), float(db_lng))
            level = 'manual'
            print(f"[{i}/{len(rows)}] {name} -> 使用数据库坐标")
            reused += 1
        else:
            cached = coord_cache.get(row['id'])
            if cached:
                coords = (cached['lat'], cached['lng'])
                level = cached['geo_level']
                print(f"[{i}/{len(rows)}] {name} -> 复用坐标 ({level})")
                reused += 1
            else:
                print(f"[{i}/{len(rows)}] Geocoding: {name}...", end=' ', flush=True)
                coords, level = get_precise_coords(row['English_name'] or name, location)
                if coords:
                    print(f"OK ({level}) -> {coords[0]:.4f}, {coords[1]:.4f}")
                    geocoded += 1
                else:
                    print("FAILED")
                    failed.append(name)

        lines = row['production_lines']
        if isinstance(lines, str):
            try:
                lines = json.loads(lines)
            except Exception:
                lines = None
        lines = normalize_production_lines(lines)

        annual_cap = row.get('annual_production_capacity')
        annual_vessel = None
        annual_dwt = None
        if isinstance(annual_cap, str):
            try:
                annual_cap = json.loads(annual_cap)
            except Exception:
                annual_cap = None
        if isinstance(annual_cap, dict):
            annual_vessel = annual_cap.get('vessel')
            annual_dwt = annual_cap.get('specs')

        update_time = row.get('update_time')
        update_time_str = str(update_time).strip() if update_time else ''
        is_verified = bool(row.get('is_verified'))

        boundary_geo = row.get('boundary_geo')
        if isinstance(boundary_geo, str):
            try:
                boundary_geo = json.loads(boundary_geo)
            except Exception:
                boundary_geo = None

        branch_base = row.get('branch_base')
        if isinstance(branch_base, str):
            try:
                branch_base = json.loads(branch_base)
            except Exception:
                branch_base = None
        if isinstance(branch_base, dict) and branch_base.get('docks'):
            branch_base['docks'] = normalize_production_lines(branch_base['docks'])

        shipyards.append({
            'id': row['id'],
            'name': name,
            'lat': coords[0] if coords else 0,
            'lng': coords[1] if coords else 0,
            'location': location,
            'refund': row['refund_guarantee_capacity'] or '未知',
            'update_time': update_time_str,
            'update_person': row.get('update_person') or '',
            'is_verified': is_verified,
            'person_in_charge': row.get('person_in_charge') or '',
            'annual_vessel': annual_vessel,
            'annual_dwt': annual_dwt,
            'lines': lines or [],
            'geo_level': level if coords else 'failed',
            'is_active': row.get('is_active') or 'Y',
            'shipyard_type': row.get('shipyard_type') or '',
            'boundary': boundary_geo,
            'employees': row.get('employees') if row.get('employees') is not None else 0,
            'utilization': row.get('utilization_rate') if row.get('utilization_rate') is not None else 0,
            'orderbook_months': row.get('orderbook_months') if row.get('orderbook_months') is not None else 0,
            'founded': row.get('founded_year') if row.get('founded_year') is not None else 0,
            'former_name': row.get('former_name') or '',
            'state_owned': row.get('state_owned') or '',
            'shipyard_area': row.get('shipyard_area') or '',
            'Quayside': row.get('Quayside') or '',
            'Block_Assembly_Workshop': row.get('Block_Assembly_Workshop') or '',
            'painting_workshop': row.get('painting_workshop') or '',
            'Outfitting_workshop': row.get('Outfitting_workshop') or '',
            'has_branch': row.get('has_branch') or 'N',
            'branch': branch_base if branch_base else None,
        })

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(shipyards, f, ensure_ascii=False, indent=2, cls=_DecimalEncoder)

    print(f"\n[完成] 已生成: {output_path}")
    print(f"总计: {len(shipyards)} 个船厂")
    if failed:
        print(f"失败: {len(failed)} 个")
        for n in failed:
            print(f"  - {n}")

    summary = {
        "total": len(shipyards),
        "geocoded": geocoded,
        "reused": reused,
        "failed": len(failed),
        "failed_names": failed,
        "output_path": output_path,
    }
    return shipyards, summary


def sync_shipyards(output_path: str | None = None):
    """Compare DB with the existing map JSON and synchronize.

    Behavior:
    - Shipyards present in DB but not in JSON are added (geocoded if no coords).
    - Shipyards present in JSON but not in DB are removed.
    - Existing shipyards keep their manually edited coordinates; other fields
      are updated from the DB when they differ.
    - Returns (shipyards_list, summary_dict).
    """
    if output_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_path = os.path.join(script_dir, '..', '..', 'data', 'shipyards_data.json')
        output_path = os.path.abspath(output_path)

    print("[DB] 连接数据库进行同步...")
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        'SELECT id, Local_name, English_name, location, refund_guarantee_capacity, production_lines, '
        'annual_production_capacity, update_time, update_person, is_verified, person_in_charge, is_active, shipyard_type, lat, lng, boundary_geo, '
        'employees, utilization_rate, orderbook_months, founded_year, '
        'former_name, state_owned, shipyard_area, Quayside, Block_Assembly_Workshop, painting_workshop, Outfitting_workshop, '
        'has_branch, branch_base '
        'FROM shipyard_data WHERE location IS NOT NULL AND location != %s',
        ('',)
    )
    db_rows = cursor.fetchall()
    cursor.close()
    conn.close()
    print(f"[DB] 获取到 {len(db_rows)} 个船厂")

    existing = []
    if os.path.exists(output_path):
        try:
            with open(output_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except Exception as e:
            print(f"[WARN] 读取现有 JSON 失败: {e}")
    print(f"[JSON] 现有 {len(existing)} 个船厂")

    existing_by_id = {s.get('id'): s for s in existing if s.get('id') is not None}
    existing_by_name = {s.get('name'): s for s in existing if s.get('name')}

    shipyards = []
    added = []
    updated = []
    unchanged = []
    failed = []

    db_ids = set()
    for row in db_rows:
        sid = row['id']
        db_ids.add(sid)
        name = row['Local_name'] or row['English_name'] or f'Shipyard #{sid}'
        location = row['location']

        existing_entry = existing_by_id.get(sid)
        if not existing_entry:
            existing_entry = existing_by_name.get(name)

        lines = row['production_lines']
        if isinstance(lines, str):
            try:
                lines = json.loads(lines)
            except Exception:
                lines = None
        lines = normalize_production_lines(lines)

        annual_cap = row.get('annual_production_capacity')
        annual_vessel = None
        annual_dwt = None
        if isinstance(annual_cap, str):
            try:
                annual_cap = json.loads(annual_cap)
            except Exception:
                annual_cap = None
        if isinstance(annual_cap, dict):
            annual_vessel = annual_cap.get('vessel')
            annual_dwt = annual_cap.get('specs')

        update_time = row.get('update_time')
        update_time_str = str(update_time).strip() if update_time else ''
        is_verified = bool(row.get('is_verified'))

        db_lat = row.get('lat')
        db_lng = row.get('lng')
        db_boundary = row.get('boundary_geo')
        if isinstance(db_boundary, str):
            try:
                db_boundary = json.loads(db_boundary)
            except Exception:
                db_boundary = None

        db_branch = row.get('branch_base')
        if isinstance(db_branch, str):
            try:
                db_branch = json.loads(db_branch)
            except Exception:
                db_branch = None
        if isinstance(db_branch, dict) and db_branch.get('docks'):
            db_branch['docks'] = normalize_production_lines(db_branch['docks'])

        if existing_entry:
            # DB coordinates take precedence over cached/geocoded ones
            if db_lat is not None and db_lng is not None:
                coords = (float(db_lat), float(db_lng))
                level = 'manual'
            elif existing_entry.get('lat') and existing_entry.get('lng'):
                coords = (existing_entry.get('lat'), existing_entry.get('lng'))
                level = existing_entry.get('geo_level', 'manual')
            else:
                coords = None
                level = 'failed'
            has_coords = coords is not None
            json_boundary = existing_entry.get('boundary')
            boundary = db_boundary if db_boundary is not None else json_boundary

            new_entry = {
                'id': sid,
                'name': name,
                'lat': coords[0] if coords else 0,
                'lng': coords[1] if coords else 0,
                'location': location,
                'refund': row['refund_guarantee_capacity'] or '未知',
                'update_time': update_time_str,
                'update_person': row.get('update_person') or '',
                'is_verified': is_verified,
                'person_in_charge': row.get('person_in_charge') or '',
                'annual_vessel': annual_vessel,
                'annual_dwt': annual_dwt,
                'lines': lines or [],
                'geo_level': level if coords else 'failed',
                'is_active': row.get('is_active') or 'Y',
                'shipyard_type': row.get('shipyard_type') or '',
                'boundary': boundary,
                'employees': row.get('employees') if row.get('employees') is not None else 0,
                'utilization': row.get('utilization_rate') if row.get('utilization_rate') is not None else 0,
                'orderbook_months': row.get('orderbook_months') if row.get('orderbook_months') is not None else 0,
                'founded': row.get('founded_year') if row.get('founded_year') is not None else 0,
                'former_name': row.get('former_name') or '',
                'state_owned': row.get('state_owned') or '',
                'shipyard_area': row.get('shipyard_area') or '',
                'Quayside': row.get('Quayside') or '',
                'Block_Assembly_Workshop': row.get('Block_Assembly_Workshop') or '',
                'painting_workshop': row.get('painting_workshop') or '',
                'Outfitting_workshop': row.get('Outfitting_workshop') or '',
                'has_branch': row.get('has_branch') or 'N',
                'branch': db_branch if db_branch else None,
            }

            changed = (
                existing_entry.get('name') != name or
                existing_entry.get('location') != location or
                existing_entry.get('refund') != new_entry['refund'] or
                existing_entry.get('update_time') != update_time_str or
                existing_entry.get('update_person') != new_entry['update_person'] or
                existing_entry.get('is_verified') != is_verified or
                existing_entry.get('person_in_charge') != new_entry['person_in_charge'] or
                existing_entry.get('lines') != new_entry['lines'] or
                existing_entry.get('annual_vessel') != new_entry['annual_vessel'] or
                existing_entry.get('annual_dwt') != new_entry['annual_dwt'] or
                existing_entry.get('is_active') != new_entry['is_active'] or
                existing_entry.get('shipyard_type') != new_entry['shipyard_type'] or
                existing_entry.get('boundary') != boundary or
                existing_entry.get('employees') != new_entry['employees'] or
                existing_entry.get('utilization') != new_entry['utilization'] or
                existing_entry.get('orderbook_months') != new_entry['orderbook_months'] or
                existing_entry.get('founded') != new_entry['founded'] or
                existing_entry.get('former_name') != new_entry['former_name'] or
                existing_entry.get('state_owned') != new_entry['state_owned'] or
                existing_entry.get('shipyard_area') != new_entry['shipyard_area'] or
                existing_entry.get('Quayside') != new_entry['Quayside'] or
                existing_entry.get('Block_Assembly_Workshop') != new_entry['Block_Assembly_Workshop'] or
                existing_entry.get('painting_workshop') != new_entry['painting_workshop'] or
                existing_entry.get('Outfitting_workshop') != new_entry['Outfitting_workshop'] or
                existing_entry.get('has_branch') != new_entry['has_branch'] or
                existing_entry.get('branch') != new_entry['branch']
            )
            if changed:
                updated.append(name)
            else:
                unchanged.append(name)
        else:
            # New shipyard from DB: try to geocode
            print(f"[新增] Geocoding: {name}...", end=' ', flush=True)
            coords, level = get_precise_coords(row['English_name'] or name, location)
            if coords:
                print(f"OK ({level}) -> {coords[0]:.4f}, {coords[1]:.4f}")
            else:
                print("FAILED")
                failed.append(name)

            new_entry = {
                'id': sid,
                'name': name,
                'lat': coords[0] if coords else 0,
                'lng': coords[1] if coords else 0,
                'location': location,
                'refund': row['refund_guarantee_capacity'] or '未知',
                'update_time': update_time_str,
                'update_person': row.get('update_person') or '',
                'is_verified': is_verified,
                'person_in_charge': row.get('person_in_charge') or '',
                'annual_vessel': annual_vessel,
                'annual_dwt': annual_dwt,
                'lines': lines or [],
                'geo_level': level if coords else 'failed',
                'is_active': row.get('is_active') or 'Y',
                'shipyard_type': row.get('shipyard_type') or '',
                'boundary': db_boundary,
                'employees': row.get('employees') if row.get('employees') is not None else 0,
                'utilization': row.get('utilization_rate') if row.get('utilization_rate') is not None else 0,
                'orderbook_months': row.get('orderbook_months') if row.get('orderbook_months') is not None else 0,
                'founded': row.get('founded_year') if row.get('founded_year') is not None else 0,
                'former_name': row.get('former_name') or '',
                'state_owned': row.get('state_owned') or '',
                'shipyard_area': row.get('shipyard_area') or '',
                'Quayside': row.get('Quayside') or '',
                'Block_Assembly_Workshop': row.get('Block_Assembly_Workshop') or '',
                'painting_workshop': row.get('painting_workshop') or '',
                'Outfitting_workshop': row.get('Outfitting_workshop') or '',
                'has_branch': row.get('has_branch') or 'N',
                'branch': db_branch if db_branch else None,
            }
            added.append(name)

        shipyards.append(new_entry)

    removed = [s.get('name') for s in existing if s.get('id') not in db_ids]

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(shipyards, f, ensure_ascii=False, indent=2, cls=_DecimalEncoder)

    print(f"\n[完成] 已同步: {output_path}")
    print(f"总计: {len(shipyards)} 个船厂")
    if added:
        print(f"新增: {len(added)} 个")
    if updated:
        print(f"更新: {len(updated)} 个")
    if removed:
        print(f"删除: {len(removed)} 个")
    if failed:
        print(f"地理编码失败: {len(failed)} 个")

    summary = {
        "total": len(shipyards),
        "added": len(added),
        "updated": len(updated),
        "removed": len(removed),
        "unchanged": len(unchanged),
        "failed": len(failed),
        "added_names": added,
        "updated_names": updated,
        "removed_names": removed,
        "failed_names": failed,
        "output_path": output_path,
    }
    return shipyards, summary


def main():
    fetch_shipyards()


if __name__ == '__main__':
    main()

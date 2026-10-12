"""
批量迁移脚本：将 production_lines 中的旧格式单数 `crane` 转换为新格式 `cranes` 数组

功能：
- 遍历数据库中所有 production_lines
- 如果产线中使用的是旧格式 `crane`（单数字典），则转换为 `cranes`（数组）
- 如果已经是新格式 `cranes`（数组），则跳过
- 更新数据库

运行方式：
    python adjust/migrate_crane_to_cranes.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import mysql.connector
from mysql.connector import Error

try:
    from ai_extract_shipyard_info import DB_CONFIG
except ImportError:
    from config import DB_CONFIG


def connect_db():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        print("[DB] 数据库连接成功")
        return conn
    except Error as e:
        print(f"[DB] 数据库连接失败: {e}")
        return None


def migrate_shipyard(conn, shipyard_id: int, name: str, production_lines):
    """将单个船厂的旧格式 crane 迁移为 cranes 数组"""
    if not production_lines:
        return False, "无 production_lines 数据"

    try:
        lines = json.loads(production_lines) if isinstance(production_lines, str) else production_lines
    except Exception as e:
        return False, f"JSON 解析失败: {e}"

    if not isinstance(lines, list):
        return False, "production_lines 不是列表"

    modified = False
    for line in lines:
        if not isinstance(line, dict):
            continue

        # 旧格式：单数 crane 对象
        if 'crane' in line and 'cranes' not in line:
            old_crane = line.pop('crane')
            if isinstance(old_crane, dict):
                # 确保 capacity 是纯数字
                cap = old_crane.get('capacity')
                if cap is not None:
                    try:
                        cap_num = float(str(cap).replace(',', ''))
                        old_crane['capacity'] = int(cap_num) if cap_num == int(cap_num) else cap_num
                    except (ValueError, TypeError):
                        pass
                # 确保 no 是整数
                c_no = old_crane.get('no')
                if c_no is not None:
                    try:
                        old_crane['no'] = int(c_no)
                    except (ValueError, TypeError):
                        pass
                line['cranes'] = [old_crane]
            else:
                line['cranes'] = []
            modified = True

        # 如果既没有 crane 也没有 cranes，补充空数组
        elif 'cranes' not in line:
            line['cranes'] = []
            modified = True

    if not modified:
        return False, "已经是新格式，无需迁移"

    # 更新数据库
    cursor = conn.cursor()
    sql = "UPDATE shipyard_data SET production_lines = %s WHERE id = %s"
    cursor.execute(sql, (json.dumps(lines, ensure_ascii=False), shipyard_id))
    conn.commit()
    cursor.close()

    # 统计信息
    total_cranes = sum(len(line.get('cranes', [])) for line in lines if isinstance(line.get('cranes'), list))
    return True, f"迁移成功，共 {len(lines)} 条产线，{total_cranes} 台吊机"


def main():
    conn = connect_db()
    if not conn:
        return

    cursor = conn.cursor()
    cursor.execute("SELECT id, English_name, Local_name, production_lines FROM shipyard_data WHERE production_lines IS NOT NULL")
    rows = cursor.fetchall()
    cursor.close()

    print(f"\n共找到 {len(rows)} 条需要检查的记录\n")

    success = 0
    skipped = 0
    failed = 0

    for row in rows:
        sid = row[0]
        name = row[1] or row[2] or f"ID:{sid}"

        ok, msg = migrate_shipyard(conn, sid, name, row[3])

        if ok:
            print(f"[已迁移] {name} (ID:{sid}) - {msg}")
            success += 1
        elif "已经是新格式" in msg:
            print(f"[已跳过] {name} (ID:{sid}) - {msg}")
            skipped += 1
        else:
            print(f"[失败] {name} (ID:{sid}) - {msg}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"迁移完成: 成功={success}, 跳过={skipped}, 失败={failed}")
    print(f"{'='*60}")

    conn.close()
    print("[DB] 数据库连接已关闭")


if __name__ == '__main__':
    main()

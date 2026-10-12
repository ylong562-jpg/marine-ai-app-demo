#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迁移 shipyard_data.annual_production_capacity 从自由文本 varchar 到 JSON 格式。

新格式：{"vessel": "10-20" | "10" | null, "specs": 3000000 | null}
- vessel：年造船数，范围用 "min-max" 表示，单值用数字字符串。
- specs：年生产总吨，单位 DWT，纯数字。

运行前会自动备份旧值到 backend/data/annual_capacity_backup_<timestamp>.json。
支持 --dry-run 预览解析结果。
"""

import sys
import os
import json
import argparse
from datetime import datetime

# 确保能导入 shipyard_ai 下的规范化函数
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPYARD_AI_ROOT = os.path.join(PROJECT_ROOT, "shipyard_ai")
if SHIPYARD_AI_ROOT not in sys.path:
    sys.path.insert(0, SHIPYARD_AI_ROOT)

import mysql.connector
from ai_extract_shipyard_info import _normalize_annual_production_capacity

DB_CONFIG = {
    'host': 'your-db-host.example.com',
    'user': 'your-db-user',
    'password': 'your-db-password',
    'database': 'Time_Series',
    'port': 3306
}


def backup(conn, backup_path: str) -> list:
    """备份当前 annual_production_capacity 旧值。"""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        'SELECT id, Local_name, English_name, annual_production_capacity '
        'FROM shipyard_data'
    )
    rows = cursor.fetchall()
    cursor.close()

    backup_data = []
    for r in rows:
        v = r['annual_production_capacity']
        if v is not None and str(v).strip() != '':
            backup_data.append({
                'id': r['id'],
                'Local_name': r['Local_name'],
                'English_name': r['English_name'],
                'annual_production_capacity': v if isinstance(v, str) else str(v),
            })

    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump(backup_data, f, ensure_ascii=False, indent=2)
    print(f'[BACKUP] {len(backup_data)} 条旧值已备份到 {backup_path}')
    return backup_data


def alter_column(conn, dry_run: bool = False):
    """将 annual_production_capacity 列改为 JSON 类型。"""
    cursor = conn.cursor(dictionary=True)
    cursor.execute('DESCRIBE shipyard_data')
    cols = {row['Field']: row['Type'] for row in cursor.fetchall()}
    cursor.close()

    current_type = cols.get('annual_production_capacity', '').lower()
    if 'json' in current_type:
        print('[ALTER] annual_production_capacity 已经是 JSON 类型，跳过')
        return

    # 空字符串无法直接转为 JSON，先置空
    sql_clear = "UPDATE shipyard_data SET annual_production_capacity = NULL WHERE annual_production_capacity = ''"
    sql_alter = 'ALTER TABLE shipyard_data MODIFY annual_production_capacity JSON NULL'

    print(f'[ALTER] {sql_clear}')
    print(f'[ALTER] {sql_alter}')
    if dry_run:
        return

    cursor = conn.cursor()
    try:
        cursor.execute(sql_clear)
        cursor.execute(sql_alter)
        conn.commit()
        print('[ALTER] 改列成功')
    except Exception as e:
        conn.rollback()
        raise
    finally:
        cursor.close()


def migrate(conn, dry_run: bool = False):
    """解析旧值并更新为 JSON 格式。"""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        'SELECT id, Local_name, annual_production_capacity FROM shipyard_data '
        'WHERE annual_production_capacity IS NOT NULL'
    )
    rows = cursor.fetchall()
    cursor.close()

    updates = []
    for r in rows:
        old_val = r['annual_production_capacity']
        if old_val is None:
            continue

        if isinstance(old_val, dict):
            normalized = _normalize_annual_production_capacity(old_val)
        else:
            s = str(old_val).strip()
            if s == '' or s.lower() in ('null', 'none'):
                normalized = {"vessel": None, "specs": None}
            else:
                normalized = _normalize_annual_production_capacity(s)

        updates.append((r['id'], r['Local_name'], old_val, normalized))

    if dry_run:
        print(f'[DRY-RUN] 将更新 {len(updates)} 条记录，示例如下：')
        for uid, name, old, new in updates[:15]:
            print(f'  id={uid} | {name}: {old!r} -> {new}')
        return

    cursor = conn.cursor()
    updated = 0
    errors = 0
    for uid, name, old, new in updates:
        json_str = json.dumps(new, ensure_ascii=False)
        try:
            cursor.execute(
                'UPDATE shipyard_data SET annual_production_capacity = %s WHERE id = %s',
                (json_str, uid)
            )
            updated += cursor.rowcount
        except Exception as e:
            errors += 1
            print(f'[ERROR] id={uid} ({name}) 更新失败: {e}; value={json_str}')
    conn.commit()
    cursor.close()
    print(f'[MIGRATE] 成功更新 {updated} 条记录，失败 {errors} 条')


def main():
    parser = argparse.ArgumentParser(
        description='迁移 annual_production_capacity 到 JSON 格式（vessel + specs）'
    )
    parser.add_argument('--dry-run', action='store_true', help='仅预览，不执行改列和更新')
    args = parser.parse_args()

    data_dir = os.path.join(PROJECT_ROOT, '..', '..', 'data')
    data_dir = os.path.abspath(data_dir)
    backup_path = os.path.join(
        data_dir,
        f'annual_capacity_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    )

    conn = mysql.connector.connect(**DB_CONFIG)
    try:
        backup(conn, backup_path)
        # 先在 varchar 列上将所有旧值写成合法 JSON 字符串，再改列类型
        migrate(conn, dry_run=args.dry_run)
        alter_column(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == '__main__':
    main()

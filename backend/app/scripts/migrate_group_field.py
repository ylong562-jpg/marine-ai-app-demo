# -*- coding: utf-8 -*-
"""
一次性迁移脚本：将 shipyard_data.group 字段规范化为四个选项之一。

规范值：
  - COSCO（中远海运）
  - CSSC（中船集团）
  - CMG（招商船舶）
  - 其它

逻辑：
  1. 读取所有 group 非空的船厂记录。
  2. 对 group 值调用字段标准化逻辑。
  3. 若发生变化，则更新 shipyard_data 并写入 shipyard_update_history。

使用方式：
    cd <repo>/backend
    source .venv/bin/activate
    python app/scripts/migrate_group_field.py [--dry-run]
"""
import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings
from app.shipyard_ai.field_specs import _normalize_group

try:
    import mysql.connector

    MYSQL_AVAILABLE = True
except ImportError:  # pragma: no cover
    MYSQL_AVAILABLE = False

TABLE = "shipyard_data"
HISTORY_TABLE = "shipyard_update_history"
BATCH_SIZE = 200


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _record_history(
    cursor,
    shipyard_id: int,
    update_person: str,
    update_time: str,
    changes: List[Dict[str, Any]],
) -> None:
    if not changes:
        return
    cursor.execute(
        f"""
        INSERT INTO {HISTORY_TABLE} (shipyard_id, update_time, update_person, changes)
        VALUES (%s, %s, %s, %s)
        """,
        (shipyard_id, update_time, update_person, json.dumps(changes, ensure_ascii=False)),
    )


def migrate_group_field(dry_run: bool = False) -> Dict[str, Any]:
    if not MYSQL_AVAILABLE:
        raise RuntimeError("mysql.connector is not available")

    settings = get_settings()
    config = settings.NEWS_DB_CONFIG
    conn = mysql.connector.connect(**config)
    cursor = conn.cursor(dictionary=True)

    updated = 0
    skipped = 0
    unchanged = 0
    errors: List[str] = []

    try:
        # 1. 读取所有 group 非空记录
        cursor.execute(f"SELECT id, Local_name, `group` FROM {TABLE} WHERE `group` IS NOT NULL AND `group` != ''")
        rows = cursor.fetchall()

        print(f"[Migrate] 共发现 {len(rows)} 条 group 非空记录")

        for row in rows:
            row_id = row["id"]
            local_name = row.get("Local_name") or ""
            old_value = str(row["group"]).strip()
            new_value = _normalize_group(old_value)

            if new_value == old_value:
                unchanged += 1
                continue

            if dry_run:
                print(f"[DryRun] id={row_id} name={local_name!r}: {old_value!r} -> {new_value!r}")
                updated += 1
                continue

            try:
                now = _now()
                cursor.execute(
                    f"UPDATE {TABLE} SET `group` = %s WHERE id = %s",
                    (new_value, row_id),
                )
                _record_history(
                    cursor,
                    row_id,
                    "system_group_migration",
                    now,
                    [{"field": "group", "old": old_value, "new": new_value}],
                )
                updated += 1

                # 批量提交
                if updated % BATCH_SIZE == 0:
                    conn.commit()
                    print(f"[Migrate] 已处理 {updated} 条...")
            except Exception as e:
                errors.append(f"id={row_id}: {e}")
                skipped += 1

        if not dry_run:
            conn.commit()

        print(f"[Migrate] 完成：更新 {updated} 条，未变化 {unchanged} 条，跳过 {skipped} 条，错误 {len(errors)} 条")
        if errors:
            for err in errors[:20]:
                print(f"  - {err}")

        return {
            "success": True,
            "dry_run": dry_run,
            "total": len(rows),
            "updated": updated,
            "unchanged": unchanged,
            "skipped": skipped,
            "errors": errors,
        }
    finally:
        cursor.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="规范化 shipyard_data.group 字段")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不写入数据库")
    args = parser.parse_args()

    result = migrate_group_field(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

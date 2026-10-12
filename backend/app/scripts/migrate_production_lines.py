# -*- coding: utf-8 -*-
"""
迁移脚本：为 production_lines 补齐新字段并规范化 dimensions 格式。

运行方式：
    cd <repo>/backend
    source .venv/bin/activate
    python app/scripts/migrate_production_lines.py
"""
import json
import os
import re
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings

settings = get_settings()
DB_CONFIG = settings.NEWS_DB_CONFIG
SHIPYARD_TABLE = "shipyard_data"
MAP_JSON_PATH = os.path.join(_PROJECT_ROOT, "data", "shipyards_data.json")


def _normalize_is_active(val):
    if isinstance(val, bool):
        return "Y" if val else "N"
    s = str(val).strip().upper() if val is not None else ""
    if s in ("N", "NO", "FALSE", "0"):
        return "N"
    return "Y"


def _normalize_primary_function(val):
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _normalize_dimensions(dim):
    if dim is None:
        return None
    s = str(dim).strip()
    if not s:
        return None
    s = re.sub(r"[xX*]", "×", s)
    s = re.sub(r"\s*×\s*", " × ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def migrate_line(line):
    if not isinstance(line, dict):
        return line

    if "primary_function" not in line:
        line["primary_function"] = None
    else:
        line["primary_function"] = _normalize_primary_function(line["primary_function"])

    line["is_active"] = _normalize_is_active(line.get("is_active"))

    if "dimensions" in line:
        line["dimensions"] = _normalize_dimensions(line["dimensions"])

    return line


def migrate_db():
    import mysql.connector

    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    updated = 0
    try:
        cursor.execute(f"SELECT id, production_lines FROM {SHIPYARD_TABLE} WHERE production_lines IS NOT NULL")
        rows = cursor.fetchall()
        for row in rows:
            sid = row["id"]
            raw = row["production_lines"]
            try:
                lines = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(lines, list):
                continue
            original = json.dumps(lines, ensure_ascii=False, sort_keys=True)
            new_lines = [migrate_line(line) for line in lines]
            if json.dumps(new_lines, ensure_ascii=False, sort_keys=True) == original:
                continue
            cursor.execute(
                f"UPDATE {SHIPYARD_TABLE} SET production_lines = %s WHERE id = %s",
                (json.dumps(new_lines, ensure_ascii=False), sid),
            )
            updated += cursor.rowcount
        conn.commit()
        print(f"[DB] 更新 {updated} 条 shipyard_data 记录的 production_lines")
    finally:
        cursor.close()
        conn.close()


def migrate_map_json():
    if not os.path.exists(MAP_JSON_PATH):
        print(f"[Map JSON] 文件不存在: {MAP_JSON_PATH}")
        return
    with open(MAP_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    changed = 0
    for sy in data:
        lines = sy.get("lines") or sy.get("production_lines")
        if not isinstance(lines, list):
            continue
        original = json.dumps(lines, ensure_ascii=False, sort_keys=True)
        new_lines = [migrate_line(line) for line in lines]
        if json.dumps(new_lines, ensure_ascii=False, sort_keys=True) != original:
            sy["lines"] = new_lines
            if "production_lines" in sy:
                sy["production_lines"] = new_lines
            changed += 1
    with open(MAP_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[Map JSON] 更新 {changed} 个船厂的 lines，保存到 {MAP_JSON_PATH}")


if __name__ == "__main__":
    migrate_db()
    migrate_map_json()

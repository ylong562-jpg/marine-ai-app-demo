# -*- coding: utf-8 -*-
"""
一次性迁移脚本：将 shipyard_name_map.json 合并到 entity_registry.db 的 shipyard 记录中。

运行方式：
    cd <repo>/backend
    source .venv/bin/activate
    python app/scripts/migrate_shipyard_name_map_to_registry.py
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.news_engine.utils.entity_code_utils import generate_code_for_entity

DATA_DIR = os.path.join(_PROJECT_ROOT, "app", "news_engine", "data")
REGISTRY_PATH = os.path.join(DATA_DIR, "entity_registry.db")
MAP_PATH = os.path.join(DATA_DIR, "shipyard_name_map.json")


def _parse_aliases(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(a).strip() for a in parsed if str(a).strip()]
    except Exception:
        pass
    return []


def _json_aliases(aliases):
    return json.dumps(aliases, ensure_ascii=False)


def main():
    if not os.path.exists(MAP_PATH):
        print(f"[SKIP] {MAP_PATH} 不存在，无需迁移")
        return
    if not os.path.exists(REGISTRY_PATH):
        raise FileNotFoundError(f"registry 不存在: {REGISTRY_PATH}")

    with open(MAP_PATH, "r", encoding="utf-8") as f:
        mapping = json.load(f)

    # 聚合 alias -> canonical
    canonical_to_aliases: dict[str, set[str]] = {}
    for alias, canonical in mapping.items():
        canonical_to_aliases.setdefault(canonical, set()).add(alias)

    conn = sqlite3.connect(REGISTRY_PATH)
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                aliases TEXT DEFAULT '[]',
                frequency INTEGER DEFAULT 1,
                created_at TEXT,
                last_seen TEXT,
                code TEXT,
                UNIQUE(entity_type, canonical_name)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(entity_type)")

        existing_canonicals: set[str] = set()
        cur.execute("SELECT canonical_name, aliases FROM entities WHERE entity_type = 'shipyard'")
        for canonical, aliases_json in cur.fetchall():
            existing_canonicals.add(canonical)
            current_aliases = set(_parse_aliases(aliases_json))
            new_aliases = canonical_to_aliases.get(canonical, set())
            merged = list(dict.fromkeys([a for a in (list(new_aliases) + list(current_aliases)) if a]))
            if new_aliases - current_aliases:
                cur.execute(
                    "UPDATE entities SET aliases = ?, last_seen = ? WHERE entity_type = 'shipyard' AND canonical_name = ?",
                    (_json_aliases(merged), now, canonical),
                )

        new_canonicals = [c for c in canonical_to_aliases if c not in existing_canonicals]
        # 预生成 code：先查询当前最大序列号
        cur.execute("SELECT MAX(CAST(SUBSTR(code, 4, 8) AS INTEGER)) FROM entities WHERE code LIKE 'PMY%'")
        seq = cur.fetchone()[0] or 0

        for canonical in sorted(new_canonicals):
            aliases = list(dict.fromkeys([a for a in canonical_to_aliases[canonical] if a]))
            if canonical not in aliases:
                aliases.insert(0, canonical)
            seq += 1
            code = f"PMY{seq:08d}X"
            cur.execute(
                """
                INSERT INTO entities (entity_type, canonical_name, aliases, frequency, created_at, last_seen, code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("shipyard", canonical, _json_aliases(aliases), 1, now, now, code),
            )

        conn.commit()

        print(f"[DONE] 已迁移 {len(mapping)} 条 alias 映射")
        print(f"       更新已有船厂: {len(existing_canonicals & set(canonical_to_aliases))}")
        print(f"       新增船厂: {len(new_canonicals)}")
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

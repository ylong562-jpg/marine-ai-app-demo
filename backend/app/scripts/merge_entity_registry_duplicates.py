# -*- coding: utf-8 -*-
"""
合并 entity_registry.db 中明显重复的规范实体名称。

使用方式（默认会先打印 dry-run 预览，确认后再执行）：
    cd <repo>/backend
    source .venv/bin/activate
    python app/scripts/merge_entity_registry_duplicates.py --dry-run
    python app/scripts/merge_entity_registry_duplicates.py --execute

合并规则：
1. 手动指定合并（MANUAL_MERGES）：例如 Ditaş -> Ditas Tankers。
2. 同 entity_type 下，规范化后完全相同的 canonical_name（忽略大小写、标点、土耳其语变音）。
   保留出现频率更高的条目作为目标；若频率相同则保留 ID 更小者。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import unicodedata
from datetime import date
from typing import Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_NEWS_ENGINE_ROOT = os.path.join(_PROJECT_ROOT, "app", "news_engine")
if _NEWS_ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _NEWS_ENGINE_ROOT)

from utils.db_config import DB_CONFIG_MYSQL

DB_PATH = os.path.join(_NEWS_ENGINE_ROOT, "data", "entity_registry.db")
BACKUP_DIR = os.path.join(_NEWS_ENGINE_ROOT, "data", "backups")

# 手动合并规则：(entity_type, source_canonical, target_canonical)
# 业务上确认：source 和 target 指向同一主体，统一使用 target 作为唯一规范名。
MANUAL_MERGES = [
    # 用户明确指定的示例
    ("shipowner", "Ditaş", "Ditas Tankers"),
    # 其他明显重复的主体（一个为另一个的变体/带前缀版本）
    ("shipowner", "CAPE SHIPPING", "Cape Shipping"),
    ("shipyard", "HD现代重工（原HD Hyundai Heavy Industries）", "HD现代重工"),
    ("shipyard", "中船广船国际有限公司", "广船国际有限公司"),
    ("shipyard", "中交四航局江门航通船业有限公司", "江门航通船业有限公司"),
]


def _normalize(text: str) -> str:
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if c.isalnum() or c.isspace()).strip()
    tr = {
        "ş": "s", "ı": "i", "i": "i", "ç": "c", "ğ": "g",
        "ö": "o", "ü": "u", "â": "a", "ă": "a", "ș": "s",
        "ț": "t", "ž": "z", "ő": "o", "ű": "u", "ń": "n",
    }
    text = "".join(tr.get(c, c) for c in text)
    return text


def _parse_aliases(aliases_text: Any) -> list[str]:
    if not aliases_text:
        return []
    try:
        parsed = json.loads(aliases_text)
        if isinstance(parsed, list):
            return [str(a).strip() for a in parsed if str(a).strip()]
    except Exception:
        pass
    return []


def _json_aliases(aliases: list[str]) -> str:
    return json.dumps(aliases, ensure_ascii=False)


def _choose_target(rows: list[dict]) -> dict:
    """在完全重复的 canonical_name 中，选择频率更高者作为目标；相同时选 ID 更小者。"""
    return sorted(rows, key=lambda r: (-r["frequency"], r["id"]))[0]


def _backup_db() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"entity_registry_{ts}.db")
    shutil.copy2(DB_PATH, backup_path)
    return backup_path


def _find_manual_pairs(cur: sqlite3.Cursor) -> list[tuple[dict, dict]]:
    pairs = []
    for entity_type, source_name, target_name in MANUAL_MERGES:
        cur.execute(
            "SELECT id, entity_type, canonical_name, aliases, frequency, created_at, last_seen, code FROM entities WHERE entity_type = ? AND canonical_name = ?",
            (entity_type, source_name),
        )
        source_row = cur.fetchone()
        cur.execute(
            "SELECT id, entity_type, canonical_name, aliases, frequency, created_at, last_seen, code FROM entities WHERE entity_type = ? AND canonical_name = ?",
            (entity_type, target_name),
        )
        target_row = cur.fetchone()
        if source_row and target_row:
            pairs.append((dict(source_row), dict(target_row)))
        elif source_row:
            print(f"[WARN] 手动合并目标不存在，跳过: {entity_type}/{target_name}")
        elif target_row:
            print(f"[INFO] 手动合并源已不存在，无需处理: {entity_type}/{source_name}")
    return pairs


def _find_exact_normalized_duplicates(cur: sqlite3.Cursor) -> list[tuple[dict, dict]]:
    cur.execute(
        "SELECT id, entity_type, canonical_name, aliases, frequency, created_at, last_seen, code FROM entities"
    )
    rows = [dict(r) for r in cur.fetchall()]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["entity_type"], _normalize(r["canonical_name"]))
        groups.setdefault(key, []).append(r)

    pairs = []
    for (entity_type, norm_name), items in groups.items():
        if len(items) <= 1:
            continue
        # 排除已被手动规则指定过的 source
        manual_sources = {
            (et, src) for et, src, _ in MANUAL_MERGES
        }
        items = [r for r in items if (entity_type, r["canonical_name"]) not in manual_sources]
        if len(items) <= 1:
            continue
        target = _choose_target(items)
        for src in items:
            if src["id"] == target["id"]:
                continue
            pairs.append((src, target))
    return pairs


def _merge_pair(cur: sqlite3.Cursor, source: dict, target: dict) -> None:
    # 合并 aliases：保留 target canonical，将 source canonical 及 source aliases 加入 target aliases
    target_aliases = _parse_aliases(target["aliases"])
    source_aliases = [source["canonical_name"]] + _parse_aliases(source["aliases"])
    merged_aliases = list(dict.fromkeys(target_aliases + source_aliases))
    # canonical_name 自身不应出现在 aliases 中
    if target["canonical_name"] in merged_aliases:
        merged_aliases.remove(target["canonical_name"])

    new_frequency = (target["frequency"] or 0) + (source["frequency"] or 0)

    def _dt_min(a, b):
        if not a:
            return b
        if not b:
            return a
        return min(a, b)

    def _dt_max(a, b):
        if not a:
            return b
        if not b:
            return a
        return max(a, b)

    new_created = _dt_min(target["created_at"], source["created_at"])
    new_last_seen = _dt_max(target["last_seen"], source["last_seen"])
    new_code = target["code"] or source["code"]

    cur.execute(
        """
        UPDATE entities
        SET aliases = ?, frequency = ?, created_at = ?, last_seen = ?, code = ?
        WHERE id = ?
        """,
        (
            _json_aliases(merged_aliases),
            new_frequency,
            new_created,
            new_last_seen,
            new_code,
            target["id"],
        ),
    )
    cur.execute("DELETE FROM entities WHERE id = ?", (source["id"],))


def _sync_mysql() -> None:
    """删除当天快照后重新同步 entity 词表，保证 MySQL 镜像与 SQLite 一致。"""
    import mysql.connector

    snapshot = date.today()
    conn = mysql.connector.connect(**DB_CONFIG_MYSQL)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM normalized_entity_vocabulary WHERE snapshot_date = %s",
            (snapshot,),
        )
        print(f"[MYSQL] 已清理 {snapshot} 的旧 entity 快照（{cursor.rowcount} 条）")

        local = sqlite3.connect(DB_PATH)
        local.row_factory = sqlite3.Row
        rows = local.execute("SELECT * FROM entities").fetchall()
        local.close()

        insert_sql = """
            INSERT INTO normalized_entity_vocabulary
            (snapshot_date, entity_id, entity_type, canonical_name, aliases, frequency, created_at, last_seen, code)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                aliases = VALUES(aliases),
                frequency = VALUES(frequency),
                created_at = VALUES(created_at),
                last_seen = VALUES(last_seen),
                code = VALUES(code),
                synced_at = VALUES(synced_at)
        """
        for row in rows:
            aliases = row["aliases"]
            try:
                aliases_json = json.dumps(json.loads(aliases), ensure_ascii=False) if aliases else None
            except Exception:
                aliases_json = json.dumps([aliases], ensure_ascii=False) if aliases else None
            cursor.execute(
                insert_sql,
                (
                    snapshot,
                    row["id"],
                    row["entity_type"],
                    row["canonical_name"],
                    aliases_json,
                    row["frequency"],
                    row["created_at"],
                    row["last_seen"],
                    row["code"],
                ),
            )
        conn.commit()
        print(f"[MYSQL] 已重新同步 {len(rows)} 条 entity 到 {snapshot} 快照")
    except Exception as e:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _print_candidates() -> None:
    """打印未被自动合并的疑似重复组合，供人工复核。"""
    import re

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT entity_type, canonical_name FROM entities")
    rows = cur.fetchall()
    conn.close()

    by_type: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        by_type.setdefault(r["entity_type"], []).append((r["canonical_name"], _normalize(r["canonical_name"])))

    print("\n[INFO] 以下组合未被自动合并，建议人工复核：")
    printed = 0
    for entity_type, items in by_type.items():
        arr = sorted(items, key=lambda x: len(x[1]))
        seen = set()
        for i, (cn1, n1) in enumerate(arr):
            if len(n1) < 5:
                continue
            for cn2, n2 in arr[i + 1 :]:
                if n1 in n2 or n2 in n1:
                    key = tuple(sorted([cn1, cn2]))
                    if key in seen:
                        continue
                    seen.add(key)
                    print(f"  {entity_type}: '{cn1}' / '{cn2}'")
                    printed += 1
    if printed == 0:
        print("  暂无")
    else:
        print(f"  共 {printed} 组候选\n")


def main():
    parser = argparse.ArgumentParser(description="合并 entity_registry.db 中的重复规范实体")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不修改数据库")
    parser.add_argument("--execute", action="store_true", help="执行合并并同步到 MySQL")
    parser.add_argument("--no-sync", action="store_true", help="执行合并但不同步 MySQL（默认会同步）")
    args = parser.parse_args()

    if not (args.dry_run or args.execute):
        parser.print_help()
        sys.exit(1)

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] 数据库不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    manual_pairs = _find_manual_pairs(cur)
    exact_pairs = _find_exact_normalized_duplicates(cur)
    all_pairs = manual_pairs + exact_pairs

    print(f"[PREVIEW] 手动指定合并: {len(manual_pairs)} 对")
    for src, tgt in manual_pairs:
        print(f"  {src['entity_type']}: '{src['canonical_name']}'({src['frequency']}) -> '{tgt['canonical_name']}'({tgt['frequency']})")
    print(f"[PREVIEW] 规范化重复合并: {len(exact_pairs)} 对")
    for src, tgt in exact_pairs:
        print(f"  {src['entity_type']}: '{src['canonical_name']}'({src['frequency']}) -> '{tgt['canonical_name']}'({tgt['frequency']})")

    if args.dry_run:
        _print_candidates()
        print("[DRY-RUN] 未做任何修改")
        conn.close()
        return

    if not all_pairs:
        print("[INFO] 没有发现需要合并的重复项")
        _print_candidates()
        conn.close()
        return

    backup_path = _backup_db()
    print(f"[BACKUP] 已备份原数据库到: {backup_path}")

    try:
        for src, tgt in all_pairs:
            print(f"[MERGE] {src['entity_type']}: '{src['canonical_name']}' -> '{tgt['canonical_name']}'")
            _merge_pair(cur, src, tgt)
        conn.commit()
        print(f"[DONE] 共合并 {len(all_pairs)} 对")
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] 合并失败，已回滚: {e}")
        raise
    finally:
        conn.close()

    if not args.no_sync:
        try:
            _sync_mysql()
        except Exception as e:
            print(f"[ERROR] MySQL 同步失败: {e}")
            raise

    _print_candidates()


if __name__ == "__main__":
    main()

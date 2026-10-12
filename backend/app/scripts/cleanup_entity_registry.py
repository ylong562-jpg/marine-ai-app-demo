# -*- coding: utf-8 -*-
"""
清理 entity_registry.db 词表。

规则：
1. 保留的 entity_type 为：shipowner, shipyard, buyer, seller, charterer, owner, company, parties
2. 将旧的 party 类型合并到 parties 类型
3. 删除其他所有类型（entity, organization, country, port, index, channel, ship 等）
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

DB_PATH = os.path.join(_PROJECT_ROOT, "app", "news_engine", "data", "entity_registry.db")

ALLOWED_TYPES = {"shipowner", "shipyard", "buyer", "seller", "charterer", "owner", "company", "parties"}


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


def cleanup():
    if not os.path.exists(DB_PATH):
        print(f"数据库不存在: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        # 1. 统计清理前
        cur.execute("SELECT entity_type, COUNT(*) FROM entities GROUP BY entity_type")
        before = dict(cur.fetchall())
        print("清理前统计:", before)

        # 2. 将 party 合并到 parties
        cur.execute("SELECT id, canonical_name, english_name, aliases, frequency, created_at, last_seen, code FROM entities WHERE entity_type = 'party'")
        party_rows = cur.fetchall()

        merged = 0
        skipped = 0
        for row in party_rows:
            _id, canonical, english, aliases, freq, created_at, last_seen, code = row
            aliases_list = _parse_aliases(aliases)
            all_names = {canonical, english} if english else {canonical}
            all_names.update(aliases_list)

            # 检查 parties 中是否已存在同名或别名冲突
            cur.execute(
                "SELECT id, canonical_name, english_name, aliases, frequency FROM entities WHERE entity_type = 'parties' AND canonical_name = ?",
                (canonical,),
            )
            existing = cur.fetchone()

            if existing:
                # 合并别名与频率
                ex_id, ex_canonical, ex_english, ex_aliases, ex_freq = existing
                ex_aliases_list = _parse_aliases(ex_aliases)
                new_aliases = list(set(ex_aliases_list + list(all_names) + ([english] if english else [])))
                new_aliases = [a for a in new_aliases if a and a != ex_canonical]
                new_freq = (ex_freq or 0) + (freq or 1)
                cur.execute(
                    "UPDATE entities SET aliases = ?, frequency = ?, last_seen = ? WHERE id = ?",
                    (json.dumps(new_aliases, ensure_ascii=False), new_freq, datetime.now().isoformat(), ex_id),
                )
                cur.execute("DELETE FROM entities WHERE id = ?", (_id,))
                merged += 1
            else:
                # 直接改为 parties 类型
                cur.execute(
                    "UPDATE entities SET entity_type = 'parties' WHERE id = ?",
                    (_id,),
                )
                merged += 1

        print(f"party -> parties 合并完成: {merged} 条")

        # 3. 删除非允许类型
        placeholders = ", ".join(["?"] * len(ALLOWED_TYPES))
        cur.execute(f"DELETE FROM entities WHERE entity_type NOT IN ({placeholders})", tuple(ALLOWED_TYPES))
        deleted = cur.rowcount
        print(f"删除非允许类型记录: {deleted} 条")

        # 4. 统计清理后
        cur.execute("SELECT entity_type, COUNT(*) FROM entities GROUP BY entity_type")
        after = dict(cur.fetchall())
        print("清理后统计:", after)

        conn.commit()
        print("词表清理完成")
    finally:
        conn.close()


if __name__ == "__main__":
    cleanup()

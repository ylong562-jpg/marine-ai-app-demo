# -*- coding: utf-8 -*-
"""
清洗并归一化三张市场分析表的 specs 字段。
- 补齐 spec_value / spec_unit 字段
- 将规格统一为 DWT/TEU 显示（或保留 CBM/CEU 等原单位）
- 对未给出具体数值的船型等级补国际通行吨位/箱位范围，并取中间值
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mysql.connector
from app.core.config import get_settings
from app.services.market_analysis_service import (
    _ensure_spec_columns,
    _ensure_market_analysis_table,
    _get_market_table,
    _MARKET_TABLES,
    _parse_specs,
)


def main():
    settings = get_settings()
    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    try:
        _ensure_market_analysis_table(conn)
        _ensure_spec_columns(conn)

        for category, table_name in _MARKET_TABLES.items():
            cursor = conn.cursor(dictionary=True)
            try:
                cursor.execute(
                    f"SELECT `id`, `ship_type`, `specs` FROM `{table_name}`"
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()

            updated = 0
            skipped = 0
            for row in rows:
                info = _parse_specs(row.get("specs"), row.get("ship_type"))
                # 只有当解析出有效结果时才更新；避免把 null 强行覆盖
                if info["spec_value"] is None and info["spec_unit"] is None and not info["specs"]:
                    skipped += 1
                    continue

                upd_cursor = conn.cursor()
                try:
                    upd_cursor.execute(
                        f"""
                        UPDATE `{table_name}`
                        SET `specs` = %s, `spec_value` = %s, `spec_unit` = %s
                        WHERE `id` = %s
                        """,
                        (info["specs"], info["spec_value"], info["spec_unit"], row["id"]),
                    )
                    conn.commit()
                    updated += 1
                finally:
                    upd_cursor.close()

            print(f"{category} ({table_name}): 总 {len(rows)} 条，更新 {updated} 条，跳过 {skipped} 条")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

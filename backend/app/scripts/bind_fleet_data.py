# -*- coding: utf-8 -*-
"""
clarkson_fleet -> shipyard_data.shipbuilding_transactions（船厂管理-建造信息字段）。

把克拉克森实际建造记录（经 pmc_builder_map 对齐）链接进船厂档案的建造信息：
- 幂等键 (source="PMC-Clarkson", pmc_table="fleet", pmc_id=clarkson_fleet.id)，
  与 bind_pmc_data.py / sync_shipyard_transactions.py 的既有约定一致；
- 与已有条目（如新闻来源）合并，按 date 倒序，每家船厂最多保留 RECENT_CAP 条
  （默认 400，防止超大船厂撑爆字段）；
- shipbuilding_transactions 列为 MEDIUMTEXT（脚本自动检查/升级，TEXT 64KB 不够）；
- event_type 用既有词汇「交付」（建造完成即交付），管理页下拉可正常显示。

用法（backend 目录）：
    .venv/bin/python -m app.scripts.bind_fleet_data            # dry-run（默认）
    .venv/bin/python -m app.scripts.bind_fleet_data --apply   # 写入
"""
import argparse
import json
import sys

import mysql.connector

from app.core.config import get_settings

SOURCE = "PMC-Clarkson"
PM_TABLE = "fleet"
RECENT_CAP = 400
COLLATE = "COLLATE utf8mb4_unicode_ci"


def _tx_key(tx: dict) -> tuple:
    return (
        str(tx.get("source") or ""),
        str(tx.get("pmc_table") or ""),
        str(tx.get("pmc_id") or ""),
    )


def _parse(value) -> list:
    if isinstance(value, list):
        return [t for t in value if isinstance(t, dict)]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return [t for t in parsed if isinstance(t, dict)] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def ensure_mediumtext(cur) -> bool:
    cur.execute(
        """
        SELECT DATA_TYPE FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = 'shipyard_data'
          AND column_name = 'shipbuilding_transactions'
        """
    )
    row = cur.fetchone()
    dtype = (row.get("DATA_TYPE") or "") if isinstance(row, dict) else (row[0] if row else "")
    if dtype.lower() == "text":
        cur.execute(
            "ALTER TABLE shipyard_data "
            "MODIFY COLUMN shipbuilding_transactions MEDIUMTEXT"
        )
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="clarkson_fleet 绑定到 shipbuilding_transactions")
    parser.add_argument("--apply", action="store_true", help="写入数据库（默认 dry-run）")
    parser.add_argument("--cap", type=int, default=RECENT_CAP, help=f"每家船厂保留条数上限（默认{RECENT_CAP}）")
    args = parser.parse_args()

    conn = mysql.connector.connect(**get_settings().NEWS_DB_CONFIG)
    cur = conn.cursor(dictionary=True)
    try:
        if ensure_mediumtext(cur):
            conn.commit()
            print("✅ shipbuilding_transactions 已升级为 MEDIUMTEXT")
        else:
            print("ℹ️ shipbuilding_transactions 已是 MEDIUMTEXT/更大")

        cur.execute("SELECT id, Local_name, shipbuilding_transactions FROM shipyard_data")
        yards = cur.fetchall()
        print(f"船厂总数: {len(yards)}")

        updated = 0
        total_new = 0
        for y in yards:
            sid = y["id"]
            cur.execute(
                f"""
                SELECT f.id, f.name, f.type, f.size, f.unit, f.dwt, f.gt, f.flag,
                       f.built, f.month, f.status, f.operator, f.company, f.group_company,
                       f.engine_type, f.alternative_fuel_types, f.sox_scrubber_status,
                       f.eco_electronic_engine
                FROM clarkson_fleet f
                JOIN pmc_builder_map m ON f.builder {COLLATE} = m.builder_raw
                WHERE m.shipyard_id = %s
                ORDER BY f.built DESC, f.id DESC
                """,
                (sid,),
            )
            rows = cur.fetchall()
            if not rows:
                continue

            existing = _parse(y.get("shipbuilding_transactions"))
            existing_keys = {_tx_key(t) for t in existing}

            fleet_txs = []
            for r in rows:
                key = (SOURCE, PM_TABLE, str(r["id"]))
                if key in existing_keys:
                    continue
                built = r.get("built")
                month = r.get("month")
                date = f"{built:04d}-{month:02d}-01" if built and month else (f"{built:04d}-01-01" if built else "")
                dwt = r.get("dwt")
                size = r.get("size")
                unit = (r.get("unit") or "").strip()
                specs = f"{dwt:g} DWT" if dwt else (f"{size:g} {unit}".strip() if size else "")
                power_parts = [
                    p for p in (
                        str(r.get("alternative_fuel_types") or "").strip(),
                        str(r.get("sox_scrubber_status") or "").strip(),
                        str(r.get("eco_electronic_engine") or "").strip(),
                        str(r.get("engine_type") or "").strip(),
                    )
                    if p and p.lower() not in ("none", "no", "-", "")
                ]
                fleet_txs.append({
                    "date": date,
                    "event_type": "交付",
                    "shipowner": str(r.get("operator") or r.get("company") or r.get("group_company") or "").strip(),
                    "shipyard": str(y.get("Local_name") or "").strip(),
                    "ship_type": str(r.get("type") or "").strip(),
                    "specs": specs,
                    "vessel_name": str(r.get("name") or "").strip(),
                    "power": " / ".join(power_parts),
                    "flag": str(r.get("flag") or "").strip(),
                    "status": str(r.get("status") or "").strip(),
                    "dwt": f"{dwt:g}" if dwt else "",
                    "gt": f"{r['gt']:g}" if r.get("gt") else "",
                    "source": SOURCE,
                    "pmc_table": PM_TABLE,
                    "pmc_id": str(r["id"]),
                })

            if not fleet_txs:
                continue

            merged = existing + fleet_txs
            merged.sort(key=lambda t: str(t.get("date") or ""), reverse=True)
            capped = merged[: args.cap]
            total_new += len(fleet_txs)

            if args.apply:
                cur2 = conn.cursor()
                try:
                    cur2.execute(
                        "UPDATE shipyard_data SET shipbuilding_transactions = %s WHERE id = %s",
                        (json.dumps(capped, ensure_ascii=False), sid),
                    )
                    conn.commit()
                finally:
                    cur2.close()
            updated += 1
            if updated <= 5 or len(fleet_txs) > 200:
                print(f"  [{'写入' if args.apply else '预览'}] id={sid} {y.get('Local_name')}: "
                      f"+{len(fleet_txs)} 条, 保留 {len(capped)} 条")

        verb = "已更新" if args.apply else "可更新"
        print(f"\n✅ {verb} {updated} 家船厂，新增建造记录 {total_new} 条"
              + ("" if args.apply else "（dry-run，加 --apply 写入）"))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
PMC 数据绑定脚本：将 PMC_orderbook / PMC_fleet（Clarkson 数据副本）按
builder -> shipyard_data 的映射写入船厂管理页面的两个 JSON 字段：

  - PMC_orderbook  -> shipyard_data.newbuilding_transactions（订单信息）
  - PMC_fleet      -> shipyard_data.shipbuilding_transactions（建造信息）

匹配依据：app/scripts/pmc_builder_mapping.py 中的 AI 人工比对结果
（未收录的 builder 表示船厂数据库中无对应船厂，暂不处理）。

写入规则：
  - 船厂名字段使用 shipyard_data.Local_name（船厂数据库的中文名，不自行翻译）
  - 幂等：以 (source="PMC-Clarkson", pmc_table, pmc_id) 去重，可重复执行
  - 保留已有新闻来源交易，按 date 倒序排列

使用方式：
    cd <repo>/backend
    python app/scripts/bind_pmc_data.py --dry-run   # 预览
    python app/scripts/bind_pmc_data.py --apply     # 写入
"""
import argparse
import datetime
import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings
from app.scripts.pmc_builder_mapping import BUILDER_MAP

settings = get_settings()

SHIPYARD_TABLE = "shipyard_data"
ORDERBOOK_TABLE = "pmc_orderbook"
FLEET_TABLE = "pmc_fleet"
MAPPING_TABLE = "pmc_builder_map"
BACKUP_TABLE = "shipyard_data_backup_pmc_20260923"

SOURCE_TAG = "PMC-Clarkson"

TRADE_TYPE_MAP = {"On Order": "在手订单"}


def _get_connection():
    import mysql.connector

    return mysql.connector.connect(**settings.NEWS_DB_CONFIG)


def _fmt_num(v):
    """'23000.00' -> '23000'；None -> None"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def _fmt_date(v):
    """datetime/date -> 'YYYY-MM-DD'；str 原样返回"""
    if v is None:
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    return s or None


def _join_power(*parts):
    vals = [str(p).strip() for p in parts if p and str(p).strip()]
    return " / ".join(vals) or None


def _specs_from(row):
    dwt = _fmt_num(row.get("dwt"))
    if dwt:
        return f"{dwt} DWT"
    size = _fmt_num(row.get("size"))
    unit = (row.get("unit") or "").strip() or None
    if size and unit:
        return f"{size} {unit}"
    return size


def orderbook_to_tx(row, yard_zh):
    """PMC_orderbook 行 -> newbuilding_transactions 条目（订单信息）"""
    price = None
    nb_price = _fmt_num(row.get("nb_price"))
    currency = (row.get("currency") or "").strip() or None
    if nb_price and currency:
        price = f"{nb_price} {currency}"
    elif nb_price:
        price = nb_price
    status = (row.get("status") or "").strip() or None
    return {
        "date": _fmt_date(row.get("contract_date")),
        "trade_type": TRADE_TYPE_MAP.get(status, status),
        "shipowner": row.get("operator") or row.get("company") or row.get("group_company") or None,
        "shipyard": yard_zh,
        "ship_type": row.get("type") or None,
        "specs": _specs_from(row),
        "quantity": "1",
        "price": price,
        "delivery": (str(row.get("built")).strip() or None) if row.get("built") is not None else None,
        "power": _join_power(row.get("engine_type"), row.get("alternative_fuel_types"),
                             row.get("eco_electronic_engine"), row.get("sox_scrubber_status")),
        "hull_no": row.get("hull_no") or None,
        "dwt": _fmt_num(row.get("dwt")),
        "gt": _fmt_num(row.get("gt")),
        "cgt": _fmt_num(row.get("cgt")),
        "group_company": row.get("group_company") or None,
        "news_id": None,
        "news_link": None,
        "news_date": None,
        "source": SOURCE_TAG,
        "pmc_table": "orderbook",
        "pmc_id": str(row.get("id")),
    }


_MONTH_NAMES = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _parse_month(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    try:
        m = int(s)
        return m if 1 <= m <= 12 else None
    except ValueError:
        return _MONTH_NAMES.get(s[:3])


def fleet_to_tx(row, yard_zh):
    """PMC_fleet 行 -> shipbuilding_transactions 条目（建造信息/现役船队）"""
    built = row.get("built")
    built_s = str(built).strip() if built is not None else ""
    date = None
    if built_s:
        m = _parse_month(row.get("month"))
        date = f"{int(float(built_s)):04d}-{m:02d}-01" if m else f"{int(float(built_s)):04d}-01-01"
    return {
        "date": date,
        "event_type": "现役",
        "shipowner": row.get("operator") or row.get("company") or row.get("group_company") or None,
        "shipyard": yard_zh,
        "ship_type": row.get("type") or None,
        "specs": _specs_from(row),
        "vessel_name": row.get("name") or None,
        "power": _join_power(row.get("engine_type"), row.get("alternative_fuel_types"),
                             row.get("eco_electronic_engine"), row.get("sox_scrubber_status")),
        "flag": row.get("flag") or None,
        "news_id": None,
        "news_link": None,
        "news_date": None,
        "source": SOURCE_TAG,
        "pmc_table": "fleet",
        "pmc_id": str(row.get("id")),
    }


def _parse_json_safe(value):
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def _tx_key(tx):
    return (tx.get("source"), tx.get("pmc_table"), str(tx.get("pmc_id")))


def _sort_transactions(merged):
    """按 date 倒序，无 date 的放最后（与 sync_shipyard_transactions 一致）"""
    merged.sort(key=lambda tx: (str(tx.get("date") or "") == "", str(tx.get("date") or "")))
    merged.sort(key=lambda tx: str(tx.get("date") or ""), reverse=True)
    return merged


def _ensure_mapping_table(cur):
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {MAPPING_TABLE} (
            builder_raw VARCHAR(255) NOT NULL PRIMARY KEY,
            shipyard_id INT NOT NULL,
            shipyard_en VARCHAR(255) NULL,
            shipyard_zh VARCHAR(255) NULL,
            match_type VARCHAR(20) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def sync_mapping_table(cur, yards):
    """把 AI 比对结果同步进 pmc_builder_map（名字取自 shipyard_data 当前值）"""
    _ensure_mapping_table(cur)
    cur.execute(f"DELETE FROM {MAPPING_TABLE}")
    rows = []
    for builder, info in BUILDER_MAP.items():
        y = yards.get(info["shipyard_id"])
        if not y:
            continue
        rows.append((builder, info["shipyard_id"], y.get("English_name"), y.get("Local_name"), info["match_type"]))
    cur.executemany(
        f"INSERT INTO {MAPPING_TABLE} (builder_raw, shipyard_id, shipyard_en, shipyard_zh, match_type) "
        "VALUES (%s, %s, %s, %s, %s)", rows)
    return len(rows)


def bind(apply=False):
    conn = _get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # 备份（一次性）
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} AS SELECT * FROM {SHIPYARD_TABLE}")
        print(f"备份表: {BACKUP_TABLE}")

        cur.execute(f"SELECT id, English_name, Local_name FROM {SHIPYARD_TABLE}")
        yards = {r["id"]: r for r in cur.fetchall()}

        n_map = sync_mapping_table(cur, yards)
        print(f"映射表 {MAPPING_TABLE}: {n_map} 条 builder -> shipyard")

        stats = {"newbuilding_transactions": 0, "shipbuilding_transactions": 0,
                 "yards_touched": set(), "skipped_rows": 0}

        # 以映射表为准（数据库里可能有人工修订）
        cur.execute(f"SELECT builder_raw, shipyard_id FROM {MAPPING_TABLE}")
        builder_to_yard = {r["builder_raw"]: r["shipyard_id"] for r in cur.fetchall()}

        def collect(table, converter, col):
            placeholders = ",".join(["%s"] * len(builder_to_yard)) or "''"
            cur.execute(
                f"SELECT * FROM {table} WHERE builder IN ({placeholders})",
                tuple(builder_to_yard.keys()))
            per_yard = {}
            for row in cur.fetchall():
                yid = builder_to_yard.get(row.get("builder"))
                if yid is None or yid not in yards:
                    stats["skipped_rows"] += 1
                    continue
                yard_zh = yards[yid].get("Local_name") or yards[yid].get("English_name")
                per_yard.setdefault(yid, []).append(converter(row, yard_zh))
            for yid, txs in per_yard.items():
                stats[col] += len(txs)
                stats["yards_touched"].add(yid)
                yield yid, txs, col

        updates = []  # (shipyard_id, column, merged_json)
        yard_changes = {}  # yid -> {col: n_new}

        for yid, txs, col in collect(ORDERBOOK_TABLE, orderbook_to_tx, "newbuilding_transactions"):
            yard_changes.setdefault(yid, {})[col] = len(txs)
            updates.append((yid, col, txs))
        for yid, txs, col in collect(FLEET_TABLE, fleet_to_tx, "shipbuilding_transactions"):
            yard_changes.setdefault(yid, {})[col] = len(txs)
            updates.append((yid, col, txs))

        print(f"订单信息(newbuilding_transactions) 待写入: {stats['newbuilding_transactions']} 条")
        print(f"建造信息(shipbuilding_transactions) 待写入: {stats['shipbuilding_transactions']} 条"
              f"（fleet 表当前为空则应为 0）")
        print(f"涉及船厂: {len(stats['yards_touched'])} 个")

        if not apply:
            print("\n[dry-run] 不写入。加 --apply 执行。")
            conn.rollback()
            return

        for yid, col, new_txs in updates:
            cur.execute(
                f"SELECT {col} FROM {SHIPYARD_TABLE} WHERE id = %s", (yid,))
            existing = _parse_json_safe(cur.fetchone()[col])
            seen = {_tx_key(tx) for tx in existing
                    if tx.get("source") == SOURCE_TAG}
            fresh = [tx for tx in new_txs if _tx_key(tx) not in seen]
            merged = existing + fresh
            _sort_transactions(merged)
            cur.execute(
                f"UPDATE {SHIPYARD_TABLE} SET {col} = %s WHERE id = %s",
                (json.dumps(merged, ensure_ascii=False), yid))
            yard_changes[yid][col + "_new"] = len(fresh)

        conn.commit()
        total_new = sum(v.get("newbuilding_transactions_new", 0) + v.get("shipbuilding_transactions_new", 0)
                        for v in yard_changes.values())
        print(f"已写入 {len(updates)} 个字段更新，新增交易条目 {total_new} 条")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="绑定 PMC_orderbook/PMC_fleet 到船厂管理建造/订单信息")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    args = parser.parse_args()
    bind(apply=args.apply)

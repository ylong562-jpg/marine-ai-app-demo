# -*- coding: utf-8 -*-
"""
MySQL 数据访问层（Time_Series 库）。

连接配置来自 app.core.config 的 NEWS_DB_CONFIG（环境变量可覆盖）。
注意：pmc_orderbook/clarkson_orderbook 的 builder 列与 pmc_builder_map 的
builder_raw 列 collation 不一致（utf8mb4_0900_ai_ci vs utf8mb4_unicode_ci），
JOIN 时必须显式 COLLATE，否则报 1267 错误。
"""
import json
import re
from datetime import datetime
from typing import Any, Optional

import mysql.connector

from app.core.config import get_settings

# builder 列 collation 不一致，JOIN 时统一显式转换
COLLATE_FIX = "COLLATE utf8mb4_unicode_ci"


def get_connection():
    return mysql.connector.connect(**get_settings().NEWS_DB_CONFIG)


def parse_json_loose(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# 船厂主数据
# ---------------------------------------------------------------------------

def get_shipyard(conn, shipyard_id: int) -> Optional[dict]:
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, Local_name, English_name, Abreviation, former_name "
            "FROM shipyard_data WHERE id = %s",
            (shipyard_id,),
        )
        return cursor.fetchone()
    finally:
        cursor.close()


def list_shipyards_with_orders(conn) -> list[dict]:
    """有订单映射（pmc_builder_map 命中）的船厂列表。"""
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT DISTINCT s.id, s.Local_name, s.English_name, s.Abreviation
            FROM shipyard_data s
            JOIN pmc_builder_map m ON m.shipyard_id = s.id
            ORDER BY s.id
            """
        )
        return cursor.fetchall()
    finally:
        cursor.close()


def get_name_variants(conn, shipyard: dict) -> list[str]:
    """生成用于新闻检索的船厂名称变体（去括号内容、去重、过滤过短）。"""
    variants: list[str] = []
    raw: list[str] = []
    for key in ("English_name", "Local_name", "Abreviation", "former_name"):
        val = shipyard.get(key)
        if val:
            raw.append(str(val).strip())

    # builder_raw 去掉括号部分作为核心名（如 "Armon (Vigo)" -> "Armon"）
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT builder_raw FROM pmc_builder_map WHERE shipyard_id = %s",
            (shipyard["id"],),
        )
        for row in cursor.fetchall():
            br = (row.get("builder_raw") or "").strip()
            if br:
                raw.append(br)
                no_paren = re.sub(r"[（(][^）)]*[）)]", "", br).strip()
                if no_paren and no_paren != br:
                    raw.append(no_paren)
    finally:
        cursor.close()

    seen = set()
    for name in raw:
        name = re.sub(r"\s+", " ", name).strip()
        # 去掉常见公司后缀噪音，保留核心词
        core = re.sub(
            r"(?i)\s*(co\.,?\s*ltd\.?|company limited|corporation|corp\.?|inc\.?|ltd\.?|llc|s\.a\.?)\s*$",
            "",
            name,
        ).strip(" ,.")
        for cand in (name, core):
            if len(cand) < 4:
                continue
            key = cand.lower()
            if key in seen:
                continue
            seen.add(key)
            variants.append(cand)
    return variants[:10]


def fetch_news_for_shipyard(
    conn,
    variants: list[str],
    limit: int = 200,
    min_year: int = 2023,
) -> list[dict]:
    """
    按名称变体在 title/summary/entity/data 中检索相关新闻（近期优先）。
    召回可以偏宽——LLM 分类步会过滤误召回的新闻。
    仅召回主维度为 行业新闻 / 新造船订单 的新闻（六维评分证据来源），
    避免把买卖船、租船等其他主标签新闻拉入证据集。
    """
    if not variants:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        like_clauses = []
        params: list[Any] = []
        for v in variants:
            # 用 LOCATE 子串匹配，避免 LIKE 通配符转义问题
            like_clauses.append(
                "(LOCATE(%s, title) > 0 OR LOCATE(%s, summary) > 0 "
                "OR LOCATE(%s, entity) > 0 OR LOCATE(%s, data) > 0)"
            )
            params.extend([v, v, v, v])
        cursor.execute(
            f"""
            SELECT id, title, date, summary, source_platform, tags, data, entity
            FROM ship_news
            WHERE date >= %s
              AND (JSON_CONTAINS(NULLIF(tags, ''), '"行业新闻"', '$.main_tags')
                   OR JSON_CONTAINS(NULLIF(tags, ''), '"新造船订单"', '$.main_tags'))
              AND ({' OR '.join(like_clauses)})
            ORDER BY date DESC, id DESC
            LIMIT %s
            """,
            [f"{min_year}-01-01", *params, int(limit)],
        )
        rows = cursor.fetchall()
        for r in rows:
            d = r.get("date")
            r["date"] = d.strftime("%Y-%m-%d") if isinstance(d, datetime) else str(d)[:10]
        return rows
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 订单数据（Clarkson 表 + pmc_builder_map；禁止用 ship_news 订单数据）
# ---------------------------------------------------------------------------

def fetch_orderbook_rows(conn, table: str) -> list[dict]:
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT status FROM {table} LIMIT 0")  # 表存在性检查
        cursor.fetchall()  # 消费空结果集，避免 Unread result
        cursor.execute(
            f"""
            SELECT m.shipyard_id, o.status, o.type, o.dwt, o.cgt, o.built,
                   o.builder, o.contract_date, o.nb_price, o.currency,
                   o.alternative_fuel_types, o.sox_scrubber_status, o.eco_electronic_engine
            FROM {table} o
            JOIN pmc_builder_map m ON o.builder {COLLATE_FIX} = m.builder_raw
            """
        )
        return cursor.fetchall()
    except mysql.connector.Error:
        return []
    finally:
        cursor.close()


def fetch_fleet_rows(conn) -> list[dict]:
    """clarkson_fleet 经 pmc_builder_map 对齐后的建造记录（含 shipyard_id）。"""
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT m.shipyard_id, f.built, f.month, f.dwt, f.gt, f.type,
                   f.alternative_fuel_types, f.sox_scrubber_status, f.eco_electronic_engine
            FROM clarkson_fleet f
            JOIN pmc_builder_map m ON f.builder {COLLATE_FIX} = m.builder_raw
            """
        )
        return cursor.fetchall()
    except mysql.connector.Error:
        return []
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 评分结果读写
# ---------------------------------------------------------------------------

def save_dimension_scores(conn, shipyard_id: int, rows: list[dict]):
    """
    rows: [{dimension, score, explanation, evidence_count, evidence_summary,
            model_version, computed_at}]
    主键 (shipyard_id, dimension)，已存在则覆盖更新。
    """
    cursor = conn.cursor()
    try:
        for row in rows:
            evidence_summary = row.get("evidence_summary")
            if not isinstance(evidence_summary, str):
                evidence_summary = json.dumps(evidence_summary or {}, ensure_ascii=False)
            cursor.execute(
                """
                INSERT INTO shipyard_dimension_scores
                    (shipyard_id, dimension, score, explanation, evidence_count,
                     evidence_summary, model_version, computed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    score = VALUES(score),
                    explanation = VALUES(explanation),
                    evidence_count = VALUES(evidence_count),
                    evidence_summary = VALUES(evidence_summary),
                    model_version = VALUES(model_version),
                    computed_at = VALUES(computed_at)
                """,
                (
                    shipyard_id,
                    row["dimension"],
                    float(row["score"]),
                    row.get("explanation") or "",
                    int(row.get("evidence_count") or 0),
                    evidence_summary,
                    row.get("model_version") or "",
                    row.get("computed_at") or datetime.now(),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def load_dimension_scores(conn, shipyard_id: int) -> list[dict]:
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT shipyard_id, dimension, score, explanation, evidence_count,
                   evidence_summary, model_version, computed_at
            FROM shipyard_dimension_scores
            WHERE shipyard_id = %s
            ORDER BY dimension
            """,
            (shipyard_id,),
        )
        rows = cursor.fetchall()
        for r in rows:
            if isinstance(r.get("score"), (int, float)):
                r["score"] = round(float(r["score"]), 1)
            r["evidence_summary"] = parse_json_loose(r.get("evidence_summary"))
            ca = r.get("computed_at")
            r["computed_at"] = ca.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ca, datetime) else str(ca)
        return rows
    finally:
        cursor.close()


def list_fully_computed_shipyards(conn, model_version: str) -> set[int]:
    """六个维度齐全且 model_version 匹配的船厂（用于断点续跑跳过）。"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT shipyard_id FROM shipyard_dimension_scores
            WHERE model_version = %s
            GROUP BY shipyard_id
            HAVING COUNT(DISTINCT dimension) = 6
            """,
            (model_version,),
        )
        return {int(r[0]) for r in cursor.fetchall()}
    finally:
        cursor.close()

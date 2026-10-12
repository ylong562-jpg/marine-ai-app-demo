# -*- coding: utf-8 -*-
"""
利率数据手动导入模块（临时）
==============================

用于爬虫无法访问的网页数据场景：每天将三段利率原文粘贴到本文件底部的
RAW_RATES_TEXT 变量中，然后运行本脚本，即可解析并导入 Time_Series.daily_data。

也可通过 app.api.data_import 提供的 HTTP 接口由前端页面触发。

三组数据：
1. U.S. Treasury Yields          -> US_1Y/2Y/3Y/5Y/7Y/10Y/30Y_BOND_YIELD
2. 1-month Term SOFR Swaps       -> 1Y/2Y/3Y/5Y/7Y/10Y/15Y/30Y_SOFR_swap_rate
3. Secured Overnight Financing Rate (SOFR)
                                  -> SOFR / SOFR_30_DAYS_AVG / SOFR_90_DAYS_AVG
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings

settings = get_settings()
DAILY_DATA_TABLE = "daily_data"


# ============================================================
# 每天只需更新这里：将三段利率原文直接粘贴到下方三引号之间
# ============================================================
RAW_RATES_TEXT = """
U.S. Treasury Yields

1 Year
Latest: 3.998%
06-Aug-2026: 4.052%
07-Jul-2026: 3.978%
07-Aug-2025: 3.907%

2 Year
Latest: 4.192%
06-Aug-2026: 4.243%
07-Jul-2026: 4.180%
07-Aug-2025: 3.719%

3 Year
Latest: 4.254%
06-Aug-2026: 4.300%
07-Jul-2026: 4.205%
07-Aug-2025: 3.677%

5 Year
Latest: 4.347%
06-Aug-2026: 4.386%
07-Jul-2026: 4.275%
07-Aug-2025: 3.782%

7 Year
Latest: 4.489%
06-Aug-2026: 4.519%
07-Jul-2026: 4.403%
07-Aug-2025: 3.983%

10 Year
Latest: 4.640%
06-Aug-2026: 4.665%
07-Jul-2026: 4.543%
07-Aug-2025: 4.243%

30 Year
Latest: 5.194%
06-Aug-2026: 5.210%
07-Jul-2026: 5.054%
07-Aug-2025: 4.823%

Rates provided by Chatham Financial as of 08-Aug-2026

1-month Term SOFR Swaps

1 Year
Latest: 3.927%
06-Aug-2026: 3.976%
07-Jul-2026: 3.945%
07-Aug-2025: 3.751%

2 Year
Latest: 3.970%
06-Aug-2026: 4.020%
07-Jul-2026: 3.958%
07-Aug-2025: 3.430%

3 Year
Latest: 3.971%
06-Aug-2026: 4.016%
07-Jul-2026: 3.924%
07-Aug-2025: 3.343%

5 Year
Latest: 3.990%
06-Aug-2026: 4.024%
07-Jul-2026: 3.914%
07-Aug-2025: 3.373%

7 Year
Latest: 4.046%
06-Aug-2026: 4.074%
07-Jul-2026: 3.956%
07-Aug-2025: 3.482%

10 Year
Latest: 4.148%
06-Aug-2026: 4.169%
07-Jul-2026: 4.046%
07-Aug-2025: 3.652%

15 Year
Latest: 4.311%
06-Aug-2026: 4.327%
07-Jul-2026: 4.193%
07-Aug-2025: 3.865%

30 Year
Latest: 4.358%
06-Aug-2026: 4.368%
07-Jul-2026: 4.223%
07-Aug-2025: 3.922%

Rates provided by Chatham Financial as of 08-Aug-2026

Secured Overnight Financing Rate (SOFR)

SOFR
06-Aug-2026: 3.65000%
05-Aug-2026: 3.64000%
06-Jul-2026: 3.63000%
06-Aug-2025: 4.34000%

30-Day Average SOFR
06-Aug-2026: 3.62246%
05-Aug-2026: 3.62213%
06-Jul-2026: 3.63608%
06-Aug-2025: 4.33568%

90-Day Average SOFR
06-Aug-2026: 3.62753%
05-Aug-2026: 3.62708%
06-Jul-2026: 3.63415%
06-Aug-2025: 4.34008%

1-month Term SOFR
06-Aug-2026: 3.64655%
05-Aug-2026: 3.65112%
06-Jul-2026: 3.66688%
06-Aug-2025: 4.35166%

3-month Term SOFR
06-Aug-2026: 3.75712%
05-Aug-2026: 3.75769%
06-Jul-2026: 3.72994%
06-Aug-2025: 4.23425%

Rates provided by Chatham Financial
"""


def _parse_date(date_str: str) -> str:
    """将 '09-Jul-2026' 等格式转换为 '2026-07-09'"""
    try:
        return datetime.strptime(date_str.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
    except Exception:
        return date_str.strip()


def _parse_rate(rate_str: str) -> float:
    """将 '4.060%' 等格式转换为小数 0.04060"""
    try:
        return float(rate_str.replace("%", "").replace(",", "").strip()) / 100
    except Exception:
        return 0.0


def _split_sections(text: str) -> Dict[str, str]:
    """将三段利率原文按标题拆分为三个 section"""
    titles = [
        "U.S. Treasury Yields",
        "1-month Term SOFR Swaps",
        "Secured Overnight Financing Rate (SOFR)",
    ]
    positions = []
    for title in titles:
        idx = text.find(title)
        if idx != -1:
            positions.append((idx, title))
    positions.sort()

    sections = {}
    for i, (pos, title) in enumerate(positions):
        start = pos + len(title)
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        sections[title] = text[start:end].strip()
    return sections


def _parse_section(section_text: str, term_filter: List[str] = None) -> Dict[str, List[Tuple[str, float]]]:
    """解析单个 section，返回 {term: [(date, rate), ...]}"""
    result: Dict[str, List[Tuple[str, float]]] = {}
    as_of_match = re.search(r"as of\s+(\d{2}-[A-Za-z]{3}-\d{4})", section_text)
    as_of_date = _parse_date(as_of_match.group(1)) if as_of_match else None

    current_term = None
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key.lower() == "latest" and as_of_date:
                date_str = (
                    datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=1)
                ).strftime("%Y-%m-%d")
            else:
                date_str = _parse_date(key)
            if current_term:
                result.setdefault(current_term, []).append((date_str, _parse_rate(value)))
        else:
            current_term = line

    if term_filter:
        ordered: Dict[str, List[Tuple[str, float]]] = {}
        for term in term_filter:
            if term in result:
                ordered[term] = result[term]
        return ordered
    return result


def parse_rates_text(text: str) -> Dict[str, Dict[str, List[Tuple[str, float]]]]:
    """
    解析三段利率原文。
    返回 {
        'U.S. Treasury Yields': {...},
        '1-month Term SOFR Swaps': {...},
        'Secured Overnight Financing Rate (SOFR)': {...}
    }
    """
    sections = _split_sections(text)
    return {
        "U.S. Treasury Yields": _parse_section(
            sections.get("U.S. Treasury Yields", ""),
            ["1 Year", "2 Year", "3 Year", "5 Year", "7 Year", "10 Year", "30 Year"],
        ),
        "1-month Term SOFR Swaps": _parse_section(
            sections.get("1-month Term SOFR Swaps", ""),
            ["1 Year", "2 Year", "3 Year", "5 Year", "7 Year", "10 Year", "15 Year", "30 Year"],
        ),
        "Secured Overnight Financing Rate (SOFR)": _parse_section(
            sections.get("Secured Overnight Financing Rate (SOFR)", ""),
            ["SOFR", "30-Day Average SOFR", "90-Day Average SOFR"],
        ),
    }


def _get_connection():
    import mysql.connector

    return mysql.connector.connect(**settings.NEWS_DB_CONFIG)


def _execute_query(connection, query: str):
    cursor = connection.cursor()
    try:
        cursor.execute(query)
        connection.commit()
    except Exception as e:
        connection.rollback()
        raise e
    finally:
        cursor.close()


def upload_us_yield_data(
    parsed_data: Dict[str, List[Tuple[str, float]]],
    connection: Any = None,
    dry_run: bool = False,
) -> int:
    """上传 U.S. Treasury Yields"""
    term_dict = {
        "1 Year": "US_1Y_BOND_YIELD",
        "2 Year": "US_2Y_BOND_YIELD",
        "3 Year": "US_3Y_BOND_YIELD",
        "5 Year": "US_5Y_BOND_YIELD",
        "7 Year": "US_7Y_BOND_YIELD",
        "10 Year": "US_10Y_BOND_YIELD",
        "30 Year": "US_30Y_BOND_YIELD",
    }
    count = 0
    conn = connection or _get_connection()
    try:
        for term, rates in parsed_data.items():
            column = term_dict.get(term)
            if not column:
                continue
            for date, rate in rates:
                sql_query = f"""
                    INSERT INTO {DAILY_DATA_TABLE} (date, {column})
                    VALUES ('{date}', {rate})
                    ON DUPLICATE KEY UPDATE {column} = VALUES({column});
                """
                if not dry_run:
                    _execute_query(conn, sql_query)
                count += 1
    finally:
        if connection is None:
            conn.close()
    return count


def upload_sofr_data(
    parsed_data: Dict[str, List[Tuple[str, float]]],
    connection: Any = None,
    dry_run: bool = False,
) -> int:
    """上传 1-month Term SOFR Swaps"""
    term_dict = {
        "1 Year": "1Y_SOFR_swap_rate",
        "2 Year": "2Y_SOFR_swap_rate",
        "3 Year": "3Y_SOFR_swap_rate",
        "5 Year": "5Y_SOFR_swap_rate",
        "7 Year": "7Y_SOFR_swap_rate",
        "10 Year": "10Y_SOFR_swap_rate",
        "15 Year": "15Y_SOFR_swap_rate",
        "30 Year": "30Y_SOFR_swap_rate",
    }
    count = 0
    conn = connection or _get_connection()
    try:
        for term, rates in parsed_data.items():
            column = term_dict.get(term)
            if not column:
                continue
            for date, rate in rates:
                sql_query = f"""
                    INSERT INTO {DAILY_DATA_TABLE} (date, {column})
                    VALUES ('{date}', {rate})
                    ON DUPLICATE KEY UPDATE {column} = VALUES({column});
                """
                if not dry_run:
                    _execute_query(conn, sql_query)
                count += 1
    finally:
        if connection is None:
            conn.close()
    return count


def upload_sofr_on_data(
    parsed_data: Dict[str, List[Tuple[str, float]]],
    connection: Any = None,
    dry_run: bool = False,
) -> int:
    """上传 Secured Overnight Financing Rate (SOFR)"""
    term_dict = {
        "SOFR": "SOFR",
        "30-Day Average SOFR": "SOFR_30_DAYS_AVG",
        "90-Day Average SOFR": "SOFR_90_DAYS_AVG",
    }
    count = 0
    conn = connection or _get_connection()
    try:
        for term, rates in parsed_data.items():
            column = term_dict.get(term)
            if not column:
                continue
            for date, rate in rates:
                sql_query = f"""
                    INSERT INTO {DAILY_DATA_TABLE} (date, {column})
                    VALUES ('{date}', {rate})
                    ON DUPLICATE KEY UPDATE {column} = VALUES({column});
                """
                if not dry_run:
                    _execute_query(conn, sql_query)
                count += 1
    finally:
        if connection is None:
            conn.close()
    return count


def import_interest_rates(
    raw_texts: Dict[str, str],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    解析并导入三类利率数据。

    Args:
        raw_texts: 形如 {
            "us_treasury": "U.S. Treasury Yields\n...",
            "sofr_swaps": "1-month Term SOFR Swaps\n...",
            "sofr_overnight": "Secured Overnight Financing Rate (SOFR)\n..."
        }
        dry_run: 为 True 时只解析不写入数据库

    Returns:
        {"success": bool, "us_treasury": int, "sofr_swaps": int, "sofr_overnight": int, "message": str}
    """
    # 分别解析三类文本，再合并结果。这样前端可以只粘贴其中某几段，也能正确识别。
    us_text = raw_texts.get("us_treasury", "").strip()
    sofr_text = raw_texts.get("sofr_swaps", "").strip()
    sofr_on_text = raw_texts.get("sofr_overnight", "").strip()

    us_parsed = parse_rates_text(us_text) if us_text else {}
    sofr_parsed = parse_rates_text(sofr_text) if sofr_text else {}
    sofr_on_parsed = parse_rates_text(sofr_on_text) if sofr_on_text else {}

    conn = _get_connection()
    try:
        us_count = upload_us_yield_data(
            us_parsed.get("U.S. Treasury Yields", {}), connection=conn, dry_run=dry_run
        )
        sofr_count = upload_sofr_data(
            sofr_parsed.get("1-month Term SOFR Swaps", {}), connection=conn, dry_run=dry_run
        )
        sofr_on_count = upload_sofr_on_data(
            sofr_on_parsed.get("Secured Overnight Financing Rate (SOFR)", {}),
            connection=conn,
            dry_run=dry_run,
        )
    finally:
        conn.close()

    action = "试运行完成" if dry_run else "导入成功"
    return {
        "success": True,
        "us_treasury": us_count,
        "sofr_swaps": sofr_count,
        "sofr_overnight": sofr_on_count,
        "message": (
            f"{action}：U.S. Treasury Yields {us_count} 条，"
            f"1-month Term SOFR Swaps {sofr_count} 条，"
            f"SOFR Overnight {sofr_on_count} 条。"
        ),
    }


def import_interest_rates_from_default(
    dry_run: bool = False,
) -> Dict[str, Any]:
    """使用本文件底部 RAW_RATES_TEXT 默认文本进行导入（命令行/调度用）"""
    sections = _split_sections(RAW_RATES_TEXT)
    return import_interest_rates(
        {
            "us_treasury": f"U.S. Treasury Yields\n\n{sections.get('U.S. Treasury Yields', '')}",
            "sofr_swaps": f"1-month Term SOFR Swaps\n\n{sections.get('1-month Term SOFR Swaps', '')}",
            "sofr_overnight": f"Secured Overnight Financing Rate (SOFR)\n\n{sections.get('Secured Overnight Financing Rate (SOFR)', '')}",
        },
        dry_run=dry_run,
    )


def main():
    parser = argparse.ArgumentParser(description="手动导入利率数据到 daily_data")
    parser.add_argument("--dry-run", action="store_true", help="只解析不写入数据库")
    args = parser.parse_args()

    result = import_interest_rates_from_default(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

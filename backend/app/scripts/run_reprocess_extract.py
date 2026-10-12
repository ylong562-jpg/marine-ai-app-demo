# -*- coding: utf-8 -*-
"""
Wrapper to run news reprocess extraction using marine-ai-app's own news_engine environment.
Usage:
    python run_reprocess_extract.py <input_json_path>

input_json format:
    {
        "ids": [1, 2, 3],
        "strategy": "船厂" | "关系网",
        "db_config": {...},
        "news_table": "ship_news"
    }
"""
import json
import os
import sys
import traceback
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BACKEND_ROOT = os.path.join(_PROJECT_ROOT, "app", "news_engine")
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import mysql.connector


def fetch_news_by_ids(db_config, table, ids):
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    try:
        placeholders = ", ".join(["%s"] * len(ids))
        cursor.execute(
            f"""
            SELECT id, title, link, date, source_platform, summary
            FROM {table}
            WHERE id IN ({placeholders})
            ORDER BY date DESC, id DESC
            """,
            ids,
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            date_val = r.get("date")
            date_str = ""
            if date_val:
                if isinstance(date_val, datetime):
                    date_str = date_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val)[:10]
            results.append({
                "title": r.get("title", ""),
                "link": r.get("link", ""),
                "date": date_str,
                "source_platform": r.get("source_platform", "") or "unknown",
                "summary": r.get("summary", "") or "",
            })
        return results
    finally:
        cursor.close()
        conn.close()


def _extract_shipyard_from_news(items, log_callback):
    """New shipyard reprocess pipeline using app.shipyard_ai only."""
    from app.shipyard_ai import extractor as shipyard_extractor
    from app.shipyard_ai import news_processor

    extracted_records = []
    for item in items:
        title = item.get("title", "")
        summary = item.get("summary", "")
        link = item.get("link", "")
        platform = item.get("source_platform", "") or "unknown"

        if not news_processor.is_shipyard_news(summary, title):
            log_callback(f"跳过非船厂新闻: {title[:40]}...")
            continue

        log_callback(f"提取船厂: {title[:40]}...")
        try:
            record = shipyard_extractor.extract_from_text(summary)
            if record.get("error"):
                log_callback(f"  → 提取失败: {record.get('error')}")
                continue

            # Preserve provenance.
            record["source_news_link"] = link
            record["source_platform"] = platform
            record["url"] = link
            record["raw_content"] = summary

            # Normalize against shipyard lexicon.
            record = news_processor.normalize_by_lexicon(record)
            extracted_records.append(record)
        except Exception as e:
            log_callback(f"  → 异常: {str(e)[:80]}")
            continue

    if not extracted_records:
        return []

    log_callback(f"词表归一化完成，共 {len(extracted_records)} 条提取结果")

    # Merge duplicate shipyards within the batch.
    merged = news_processor.merge_same_shipyard_in_batch(extracted_records)
    log_callback(f"批次内整合完成，合并为 {len(merged)} 个独立船厂")

    # Visual deduplication against DB (batch lookup to reduce DB load).
    results = news_processor.batch_visualize_deduplicate(merged)
    for dedup_record in results:
        status = "重复" if dedup_record.get("duplicate") else "新船厂"
        name = _candidate_name(dedup_record.get("extracted_data", {})) or "Unknown"
        log_callback(f"  → {status}: {name}")

    return results


def _candidate_name(record):
    """Small helper to get best shipyard name for logging."""
    for key in ("Local_name", "English_name"):
        value = record.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def run(input_path: str, output_path: str, log_path: str):
    with open(log_path, "a", encoding="utf-8") as log_file:
        def log(msg):
            log_file.write(f"[{datetime.now().isoformat()}] {msg}\n")
            log_file.flush()

        try:
            with open(input_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            ids = config["ids"]
            strategy = config["strategy"]
            db_config = config["db_config"]
            table = config.get("news_table", "ship_news")

            log(f"启动提取 strategy={strategy} ids={len(ids)}")
            news_items = fetch_news_by_ids(db_config, table, ids)
            if not news_items:
                _write_output(output_path, {"status": "done", "count": 0, "data": []})
                log("没有获取到新闻数据")
                return

            processor_inputs = [
                {"link": n["link"], "title": n["title"], "source_platform": n.get("source_platform", ""), "date": n.get("date", "")}
                for n in news_items
            ]

            if strategy == "关系网":
                from news_analysis.backend.relationships.extractor import extract_relationships_from_news_links
                results = extract_relationships_from_news_links(
                    processor_inputs,
                    use_async=False,
                    log_callback=lambda msg: log(msg),
                )
            else:
                results = _extract_shipyard_from_news(news_items, log)

            _write_output(output_path, {"status": "done", "count": len(results), "data": results})
            log(f"提取完成，共 {len(results)} 条结果")
        except Exception as e:
            err = traceback.format_exc()
            log(f"任务异常: {e}\n{err}")
            _write_output(output_path, {"status": "error", "count": 0, "data": [], "error": str(e)})


def _write_output(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_reprocess_extract.py <input_json_path>")
        sys.exit(1)
    input_path = sys.argv[1]
    output_path = input_path.replace("_input.json", "_result.json")
    log_path = input_path.replace("_input.json", ".log")
    run(input_path, output_path, log_path)

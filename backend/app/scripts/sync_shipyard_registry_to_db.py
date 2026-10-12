"""
Daily sync: ensure every shipyard in the entity registry has a minimal
placeholder row in MySQL shipyard_data, and update English/abbreviation if
registry has newer information.

Intended to run once per day at 08:00 via systemd timer or cron.
"""
from __future__ import annotations

import json
import logging
import mysql.connector
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

# Ensure app imports work when running as a script.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from app.core.config import get_settings
from app.news_engine.detectors.entity_deduplication import EntityRegistry
from app.shipyard_ai.deduplicator import _extract_names_from_entity

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_registry_shipyards() -> List[Dict[str, Any]]:
    registry = EntityRegistry()
    return registry._load_entities_by_type("shipyard")


def _load_db_shipyards() -> Dict[str, Dict[str, Any]]:
    """Return a dict mapping Local_name -> row fields (id, English_name, Abreviation)."""
    settings = get_settings()
    config = settings.NEWS_DB_CONFIG

    conn = mysql.connector.connect(**config)
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, Local_name, English_name, Abreviation FROM shipyard_data")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = row.get("Local_name")
        if key:
            result[key] = row
    return result


def _insert_placeholder(local_name: str, english_name: Optional[str], abbreviation: Optional[str]) -> bool:
    settings = get_settings()
    config = settings.NEWS_DB_CONFIG

    try:
        conn = mysql.connector.connect(**config)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO shipyard_data (Local_name, English_name, Abreviation, create_info)
            VALUES (%s, %s, %s, %s)
            """,
            (local_name, english_name, abbreviation, None),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as exc:
        logger.error("Failed to insert placeholder for %s: %s", local_name, exc)
        return False


def _update_names(row_id: int, english_name: Optional[str], abbreviation: Optional[str]) -> bool:
    settings = get_settings()
    config = settings.NEWS_DB_CONFIG

    try:
        conn = mysql.connector.connect(**config)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE shipyard_data
            SET English_name = COALESCE(NULLIF(English_name, ''), %s),
                Abreviation = COALESCE(NULLIF(Abreviation, ''), %s)
            WHERE id = %s
            """,
            (english_name, abbreviation, row_id),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as exc:
        logger.error("Failed to update names for id=%s: %s", row_id, exc)
        return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

    logger.info("Starting shipyard registry -> DB sync at %s", _now())

    entities = _load_registry_shipyards()
    db_map = _load_db_shipyards()

    inserted = 0
    updated = 0
    errors = 0

    for entity in entities:
        local_name, english_name, abbreviation = _extract_names_from_entity(entity)
        if not local_name:
            continue

        db_row = db_map.get(local_name)
        if db_row is None:
            if _insert_placeholder(local_name, english_name, abbreviation):
                inserted += 1
                logger.info("Inserted placeholder: %s", local_name)
            else:
                errors += 1
        else:
            # Only fill empty English_name / Abreviation from registry.
            db_english = db_row.get("English_name")
            db_abbr = db_row.get("Abreviation")
            need_update = (
                (not db_english and english_name)
                or (not db_abbr and abbreviation)
            )
            if need_update:
                if _update_names(db_row["id"], english_name, abbreviation):
                    updated += 1
                    logger.info("Updated names for: %s", local_name)
                else:
                    errors += 1

    logger.info(
        "Sync finished at %s: inserted=%d, updated=%d, errors=%d",
        _now(), inserted, updated, errors,
    )


if __name__ == "__main__":
    main()

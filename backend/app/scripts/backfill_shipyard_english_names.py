# -*- coding: utf-8 -*-
"""
一次性脚本：为 entity_registry.db 中所有 shipyard 实体补齐/规范 english_name。

执行逻辑：
1. 若 entities.english_name 字段已有有效英文值，跳过。
2. 若 canonical_name 本身就是英文，将其同时作为 english_name。
3. 否则从 aliases 中挑选最佳纯英文别名写入 english_name。
4. 若 aliases 中也没有可用英文名，调用 AI 生成并写入。
5. 清理 aliases：移除与 english_name 重复、中英混合、或已迁移到 english_name 的别名。

用法（从 backend 目录执行）：
    .venv/bin/python app/scripts/backfill_shipyard_english_names.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure app imports work when running as a script.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from app.news_engine.entity_normalizer import ShipyardNameNormalizer

REGISTRY_DB_PATH = os.path.join(
    project_root, "app", "news_engine", "data", "entity_registry.db"
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]")


def _looks_english(text: str) -> bool:
    if not text or not text.strip():
        return False
    return not bool(_CJK_RE.search(text))


def _is_mixed_alias(text: str) -> bool:
    """是否同时包含中英文（或英文+日文/韩文）字符。"""
    if not text:
        return False
    has_latin = bool(re.search(r"[a-zA-Z]", text))
    has_cjk = bool(_CJK_RE.search(text))
    return has_latin and has_cjk


def _pick_best_english_alias(aliases: List[str], abbreviation: Optional[str] = None) -> Optional[str]:
    """从 aliases 中挑选最佳英文全称（优先完整公司名，避免过短缩写）。"""
    candidates: List[str] = []
    for alias in aliases:
        if not isinstance(alias, str):
            continue
        alias = alias.strip()
        if not alias or alias == abbreviation:
            continue
        if _looks_english(alias) and len(alias) > 2:
            candidates.append(alias)
    if not candidates:
        return None
    # 优先包含 "Co., Ltd." / "Shipbuilding" / "Shipyard" 等完整后缀
    def _score(name: str) -> int:
        score = len(name)
        lower = name.lower()
        if "co., ltd." in lower or "co ltd" in lower:
            score += 50
        if "shipbuilding" in lower or "shipyard" in lower:
            score += 30
        if "heavy" in lower or "industries" in lower:
            score += 20
        return score
    return sorted(candidates, key=_score, reverse=True)[0]


def _load_shipyard_rows() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(REGISTRY_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, canonical_name, english_name, aliases FROM entities WHERE entity_type = 'shipyard'"
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _update_row(row_id: int, english_name: str, aliases: List[str]) -> None:
    conn = sqlite3.connect(REGISTRY_DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE entities
            SET english_name = ?, aliases = ?
            WHERE id = ?
            """,
            (english_name.strip(), json.dumps(aliases, ensure_ascii=False), row_id),
        )
        conn.commit()
    finally:
        conn.close()


def _clean_aliases(aliases: List[str], canonical: str, english: str) -> List[str]:
    """清理 aliases：去重、去空、去 canonical/english 本身、去中英混合。"""
    cleaned: List[str] = []
    seen = {canonical.lower(), english.lower()}
    for alias in aliases:
        if not isinstance(alias, str):
            continue
        alias = alias.strip()
        if not alias:
            continue
        key = alias.lower()
        if key in seen:
            continue
        # 丢弃中英混合别名（已有专用 english_name）
        if _is_mixed_alias(alias):
            continue
        cleaned.append(alias)
        seen.add(key)
    return cleaned


def main() -> None:
    rows = _load_shipyard_rows()
    print(f"共加载 {len(rows)} 条 shipyard 记录")

    normalizer = ShipyardNameNormalizer()

    updated = 0
    generated = 0
    skipped = 0

    for row in rows:
        row_id = row["id"]
        canonical = str(row["canonical_name"] or "").strip()
        stored_en = str(row["english_name"] or "").strip()
        aliases = []
        try:
            aliases = json.loads(row["aliases"] or "[]")
            if not isinstance(aliases, list):
                aliases = []
        except Exception:
            aliases = []

        # 1. 已有有效英文名称
        if stored_en and _looks_english(stored_en):
            cleaned = _clean_aliases(aliases, canonical, stored_en)
            if cleaned != aliases:
                _update_row(row_id, stored_en, cleaned)
                updated += 1
                print(f"[清理] {canonical} -> aliases 清理 {len(aliases)}->{len(cleaned)}")
            else:
                skipped += 1
            continue

        # 2. canonical 本身是英文
        if _looks_english(canonical):
            english = canonical
            cleaned = _clean_aliases(aliases, canonical, english)
            _update_row(row_id, english, cleaned)
            updated += 1
            print(f"[规范名即英文] {canonical}")
            continue

        # 3. 从 aliases 迁移英文名称
        best_en = _pick_best_english_alias(aliases)
        if best_en:
            cleaned = _clean_aliases(aliases, canonical, best_en)
            _update_row(row_id, best_en, cleaned)
            updated += 1
            print(f"[从别名迁移] {canonical} -> {best_en}")
            continue

        # 4. AI 生成
        generated_en = normalizer._ensure_english_name(canonical)
        if generated_en and _looks_english(generated_en):
            cleaned = _clean_aliases(aliases, canonical, generated_en)
            _update_row(row_id, generated_en, cleaned)
            generated += 1
            updated += 1
            print(f"[AI 生成] {canonical} -> {generated_en}")
        else:
            skipped += 1
            print(f"[未生成] {canonical}")

    print(f"\n处理完成：更新 {updated} 条，AI 生成 {generated} 条，跳过 {skipped} 条")


if __name__ == "__main__":
    main()

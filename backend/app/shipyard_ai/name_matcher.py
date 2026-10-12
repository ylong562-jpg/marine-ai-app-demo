"""
AI-powered shipyard name matching against the entity registry.

Provides a lightweight, synchronous wrapper that calls Qwen to decide whether
an extracted/local shipyard name corresponds to a known canonical entity.
"""

from __future__ import annotations

import difflib
import json
import logging
from typing import Any, List, Optional

from app.shipyard_ai.ai_extract_shipyard_info import call_qwen_api

logger = logging.getLogger(__name__)


def _is_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _looks_english(text: str) -> bool:
    """True if text contains Latin letters and no Chinese characters."""
    if _is_chinese(text):
        return False
    return any("\u0041" <= ch <= "\u007A" or "\u00C0" <= ch <= "\u024F" for ch in text)


def _format_candidate(entity: dict[str, Any]) -> str:
    """Format a candidate entity for the LLM prompt."""
    canonical = entity.get("canonical_name", "")
    aliases = entity.get("aliases") or []
    alias_str = ", ".join(str(a) for a in aliases if a and a != canonical)
    return f"{canonical} ({alias_str})" if alias_str else canonical


def _recall_candidates(
    local_name: Optional[str],
    english_name: Optional[str],
    entities: List[dict[str, Any]],
    top_k: int = 20,
) -> List[dict[str, Any]]:
    """Fast recall using SequenceMatcher and substring overlap."""
    queries = [q.strip() for q in (local_name, english_name) if q and q.strip()]
    if not queries or not entities:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for entity in entities:
        canonical = str(entity.get("canonical_name", "")).strip()
        aliases = entity.get("aliases") or []
        max_ratio = 0.0
        has_overlap = False

        for query in queries:
            q_lower = query.lower()
            c_lower = canonical.lower()
            ratio = difflib.SequenceMatcher(None, q_lower, c_lower).ratio()
            if ratio > max_ratio:
                max_ratio = ratio
            if q_lower in c_lower or c_lower in q_lower:
                has_overlap = True

            for alias in aliases:
                alias_text = str(alias).strip()
                if not alias_text:
                    continue
                alias_lower = alias_text.lower()
                ratio = difflib.SequenceMatcher(None, q_lower, alias_lower).ratio()
                if ratio > max_ratio:
                    max_ratio = ratio
                if q_lower in alias_lower or alias_lower in q_lower:
                    has_overlap = True

        if max_ratio >= 0.5 or has_overlap:
            scored.append((max_ratio, entity))

    scored.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for _, entity in scored:
        key = entity.get("canonical_name", "")
        if key in seen:
            continue
        seen.add(key)
        candidates.append(entity)
        if len(candidates) >= top_k:
            break

    return candidates


def ai_match_shipyard_name(
    local_name: str | None,
    english_name: str | None,
    entities: List[dict[str, Any]],
) -> str | None:
    """
    Use Qwen to match an input shipyard name to a canonical registry entity.

    Args:
        local_name: Chinese/local shipyard name (preferred).
        english_name: English shipyard name (optional).
        entities: Shipyard entities from ``EntityRegistry._load_entities_by_type("shipyard")``.

    Returns:
        The matched ``canonical_name`` or ``None`` if no confident match.
        Returns ``None`` on API failure, timeout, or invalid JSON so the caller
        can fall back to direct matching.
    """
    candidates = _recall_candidates(local_name, english_name, entities, top_k=20)
    if not candidates:
        return None

    candidate_text = "\n".join(
        f"{i + 1}. {_format_candidate(c)}" for i, c in enumerate(candidates)
    )

    input_name = ""
    if local_name:
        input_name = local_name.strip()
    if english_name:
        if input_name:
            input_name = f"{input_name} / {english_name.strip()}"
        else:
            input_name = english_name.strip()

    prompt = f"""You are a shipyard name standardization expert.
Determine which candidate shipyard the input name refers to.

Input name: {input_name}

Candidate shipyards:
{candidate_text}

Rules:
- Match based on identity (same company/shipyard), ignoring case, punctuation, word order, and generic suffixes like "集团/公司/造船/重工/船业/Shipbuilding/Group/Ltd/Inc/Co".
- If the input clearly matches one candidate, return the candidate's canonical_name.
- If none of the candidates match, return null.

Return ONLY a JSON object in this exact format:
{{"canonical_name": "..."}} or {{"canonical_name": null}}
No explanation, markdown, or additional text."""

    try:
        result_str = call_qwen_api(prompt, "", model="qwen-plus", timeout=60)
        if not result_str:
            return None

        result_str = result_str.strip()
        if result_str.startswith("```json"):
            result_str = result_str[7:]
        if result_str.startswith("```"):
            result_str = result_str[3:]
        if result_str.endswith("```"):
            result_str = result_str[:-3]
        result_str = result_str.strip()

        parsed = json.loads(result_str)
        if isinstance(parsed, dict):
            canonical = parsed.get("canonical_name")
            if canonical and isinstance(canonical, str):
                canonical = canonical.strip()
                # Guard against hallucinated names.
                for candidate in candidates:
                    if candidate.get("canonical_name") == canonical:
                        return canonical
            # Explicit null / false-ish means no match.
            return None
    except Exception as exc:
        logger.warning("AI shipyard name matching failed: %s", exc)

    return None

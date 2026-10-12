"""
Shipyard AI extraction wrappers.

Provides thin wrappers around the cleaned engine files to extract
structured shipyard information from text or supported file formats.
"""

import re
from typing import Dict, Any, List

from app.shipyard_ai.ai_extract_shipyard_info import extract_all_info
from app.shipyard_ai.file_reader import read_file


_NAME_PATTERN = re.compile(r"([^，。；、\n]{2,40}(?:造船厂|船厂|船舶|重工|集团|有限公司|股份有限公司))")


def _infer_shipyard_name(text: str) -> str:
    """Infer a concise shipyard name from the beginning of the input text.

    Falls back to the first line (truncated) if no company-like name is found.
    """
    first = str(text).strip()
    # Use only the first sentence/line for name inference
    head = re.split(r"[。！？\n]", first, maxsplit=1)[0]
    m = _NAME_PATTERN.search(head)
    if m:
        return m.group(1).strip()
    return first.split("\n")[0][:80]


def extract_from_text(text: str) -> Dict[str, Any]:
    """Extract structured shipyard information from a single text string."""
    if not text or not str(text).strip():
        return {"error": "empty text"}

    name_hint = _infer_shipyard_name(text)
    return extract_all_info(name_hint, text)


def extract_from_texts(texts: List[str]) -> List[Dict[str, Any]]:
    """Batch version of extract_from_text."""
    results = []
    for text in texts:
        results.append(extract_from_text(text))
    return results


def extract_from_file(file_path: str) -> Dict[str, Any]:
    """Read a supported file and extract structured shipyard information."""
    read_result = read_file(file_path)
    if not read_result.get("success"):
        return {
            "error": read_result.get("error", "unknown read error"),
            "file_name": read_result.get("file_name"),
        }

    content = read_result.get("content", "")
    result = extract_from_text(content)
    result["file_name"] = read_result.get("file_name")
    result["file_type"] = read_result.get("file_type")
    return result


def extract_from_files(file_paths: List[str]) -> List[Dict[str, Any]]:
    """Batch version of extract_from_file."""
    results = []
    for path in file_paths:
        results.append(extract_from_file(path))
    return results

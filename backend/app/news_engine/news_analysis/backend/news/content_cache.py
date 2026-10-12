# -*- coding: utf-8 -*-
"""
news_analysis 后端 - 新闻原文本地缓存管理

将爬取到的新闻正文以 JSON 形式缓存在本地，避免再处理时反复爬取网页。
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional


def get_cache_dir() -> str:
    """返回缓存目录的绝对路径。"""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base_dir, "data")


def get_cache_path() -> str:
    """返回新闻原文缓存文件的绝对路径。"""
    return os.path.join(get_cache_dir(), "news_content_cache.json")


def ensure_cache_dir() -> None:
    """确保缓存目录存在。"""
    os.makedirs(get_cache_dir(), exist_ok=True)


def _empty_cache() -> Dict[str, Any]:
    return {
        "version": "1.0",
        "updated_at": datetime.now().isoformat(),
        "items": [],
    }


def load_cache() -> Dict[str, Any]:
    """
    加载本地新闻原文缓存。

    Returns:
        包含 version、updated_at、items 的字典。
    """
    path = get_cache_path()
    if not os.path.exists(path):
        return _empty_cache()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "items" not in data:
            return _empty_cache()
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_cache()


def save_cache(cache: Dict[str, Any]) -> None:
    """保存缓存到本地文件。"""
    ensure_cache_dir()
    cache["updated_at"] = datetime.now().isoformat()
    path = get_cache_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _normalize_url(url: Any) -> str:
    return str(url).strip() if url else ""


def get_cache_item(url: Any) -> Optional[Dict[str, Any]]:
    """根据链接获取缓存中的完整条目。"""
    target = _normalize_url(url)
    if not target:
        return None
    cache = load_cache()
    for item in cache.get("items", []):
        if _normalize_url(item.get("link")) == target:
            return item
    return None


def get_cached_content(url: Any) -> Optional[str]:
    """根据链接获取缓存中的摘要内容。"""
    item = get_cache_item(url)
    if item is None:
        return None
    return item.get("summary") or item.get("content")


def build_cache_item(
    title: str,
    link: str,
    date: str,
    source_platform: str,
    content: str,
) -> Dict[str, Any]:
    """构造标准缓存条目。"""
    text = str(content or "").strip()
    return {
        "title": str(title or "").strip(),
        "link": str(link or "").strip(),
        "date": str(date or "").strip(),
        "source_platform": str(source_platform or "").strip(),
        "summary": text,
    }


def add_cache_item(item: Dict[str, Any], save: bool = True, check_content: bool = True) -> bool:
    """
    添加或更新单条缓存。

    Args:
        item: 缓存条目，至少包含 link 和 summary（或 content 作为兼容）。
        save: 是否立即写回文件。
        check_content: 当 URL 已存在时，是否比较内容决定是否更新；False 则 URL 存在即跳过。

    Returns:
        是否发生了新增或更新。
    """
    link = _normalize_url(item.get("link"))
    if not link:
        return False

    cache = load_cache()
    items = cache.setdefault("items", [])

    content = item.get("content") or item.get("summary", "")
    if not content or not str(content).strip():
        return False

    new_item = build_cache_item(
        title=item.get("title", ""),
        link=link,
        date=item.get("date", ""),
        source_platform=item.get("source_platform", ""),
        content=content,
    )

    for idx, existing in enumerate(items):
        if _normalize_url(existing.get("link")) == link:
            # URL 已存在时，根据 check_content 决定是否更新
            if check_content and existing.get("content") != new_item["content"]:
                items[idx] = new_item
                if save:
                    save_cache(cache)
                return True
            return False

    items.append(new_item)
    if save:
        save_cache(cache)
    return True


def add_cache_items(items: List[Dict[str, Any]], save: bool = True, check_content: bool = True) -> int:
    """
    批量添加或更新缓存条目。

    Args:
        items: 缓存条目列表。
        save: 是否立即写回文件。
        check_content: 当 URL 已存在时，是否比较内容决定是否更新；False 则 URL 存在即跳过。

    Returns:
        实际新增/更新的条目数。
    """
    if not items:
        return 0
    cache = load_cache()
    cache_items = cache.setdefault("items", [])
    link_to_idx = {_normalize_url(it.get("link")): idx for idx, it in enumerate(cache_items)}

    updated = 0
    for item in items:
        link = _normalize_url(item.get("link"))
        if not link:
            continue
        content = item.get("content") or item.get("summary", "")
        if not content or not str(content).strip():
            continue

        new_item = build_cache_item(
            title=item.get("title", ""),
            link=link,
            date=item.get("date", ""),
            source_platform=item.get("source_platform", ""),
            content=content,
        )

        if link in link_to_idx:
            idx = link_to_idx[link]
            if check_content and cache_items[idx].get("content") != new_item["content"]:
                cache_items[idx] = new_item
                updated += 1
        else:
            cache_items.append(new_item)
            link_to_idx[link] = len(cache_items) - 1
            updated += 1

    if updated and save:
        save_cache(cache)
    return updated


def cache_stats() -> Dict[str, int]:
    """返回缓存统计信息。"""
    cache = load_cache()
    return {
        "total": len(cache.get("items", [])),
    }

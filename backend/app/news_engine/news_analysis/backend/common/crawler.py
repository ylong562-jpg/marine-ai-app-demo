# -*- coding: utf-8 -*-
"""
Shared URL crawler for news analysis backends.

Moved here from news_analysis.backend.shipyard.extractor so that relationship
extractors can also use it without depending on shipyard modules.
"""
import asyncio
import traceback
from typing import Any, Dict, Optional

from content_extractor import extract_article

# Local news content cache
from news_analysis.backend.news.content_cache import (
    get_cached_content,
    add_cache_item,
)


def crawl_url(url: str, use_async: bool = True, timeout: int = 30) -> Dict[str, Any]:
    """
    爬取指定 URL 的页面内容。

    优先从本地新闻原文缓存读取；缓存未命中时再执行网络爬取，
    并将爬取结果写入缓存，供后续再处理流程复用。

    参数：
        url: 网页链接
        use_async: 是否使用异步爬虫（crawl4ai + 站点规则兜底），默认 True
        timeout: 超时时间（秒）

    返回：
        {"success": bool, "content": str, "title": str, "url": str, "error": str}
    """
    # 1. 优先读取本地缓存
    cached = get_cached_content(url)
    if cached:
        return {
            "success": True,
            "content": cached,
            "title": "",
            "url": url,
            "error": None,
            "from_cache": True,
        }

    # 2. 缓存未命中，执行网络爬取
    try:
        if use_async:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(extract_article(url=url, max_length=50000))
            finally:
                loop.close()
                asyncio.set_event_loop(None)
            crawl_result = {
                "success": result.get("success", False),
                "content": result.get("content", ""),
                "title": result.get("title", ""),
                "url": url,
                "error": result.get("error") if not result.get("success") else None,
            }
        else:
            # Fallback synchronous crawler is no longer bundled here.
            raise NotImplementedError("use_async=False is no longer supported")

        # 3. 爬取成功后将正文写入本地缓存
        if crawl_result.get("success") and crawl_result.get("content"):
            try:
                add_cache_item({
                    "title": crawl_result.get("title", ""),
                    "link": url,
                    "date": "",
                    "source_platform": "",
                    "content": crawl_result.get("content", ""),
                })
            except Exception:
                # 缓存写入失败不应影响主流程
                pass

        crawl_result["from_cache"] = False
        return crawl_result
    except Exception as e:
        return {
            "success": False,
            "content": "",
            "title": "",
            "url": url,
            "error": f"爬取异常: {str(e)}\n{traceback.format_exc()}",
        }

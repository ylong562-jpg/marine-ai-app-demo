# -*- coding: utf-8 -*-
"""
清理新闻本地缓存中的低质量条目。

质量标准（满足任一即视为低质量）：
1. summary 为空或长度 < 300 字符
2. summary 包含登录/订阅墙提示（如 TradeWinds 的 "Please login or subscribe"）
3. summary 中某一句子重复出现超过总句子数的 30%（去重失败的重复内容）
4. summary 以 "html " 开头且后续大量重复标题文字（crawl4ai 提取失败的 TradeWinds 页面）

用法：
    python clean_content_cache.py           # 预览会删除哪些 URL
    python clean_content_cache.py --execute # 执行删除
"""
import sys
import os
import re
import json
from collections import Counter
from urllib.parse import urlparse

if __name__ == "__main__" and not getattr(sys, "frozen", False):
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from news_analysis.backend.news.content_cache import load_cache, save_cache


LOGIN_WALL_PATTERNS = [
    "Please login or subscribe",
    "My Alerts is a service for subscribers",
    "This content is for subscribers",
    "Please subscribe",
    "Login to read",
    "Sign in to read",
]


def is_low_quality(item: dict) -> tuple[bool, str]:
    """判断缓存条目是否低质量，返回 (是否低质量, 原因)。"""
    text = (item.get("summary") or item.get("content") or "").strip()

    if not text:
        return True, "summary 为空"

    if len(text) < 300:
        return True, f"summary 过短（{len(text)} 字符）"

    for pattern in LOGIN_WALL_PATTERNS:
        if pattern in text:
            return True, f"包含登录/订阅墙提示：{pattern}"

    # 检测 crawl4ai 提取失败的 TradeWinds 页面：以 "html " 开头且大量重复标题
    if text.lower().startswith("html "):
        # 提取前 100 个字符作为前缀
        prefix = text[:100]
        # 如果前缀中重复词很多，认为是失败页面
        words = re.findall(r"[A-Za-z]{4,}", prefix)
        if words:
            most_common = Counter(words).most_common(1)[0]
            if most_common[1] >= 3:
                return True, "html 开头且标题重复（crawl4ai 提取失败）"

    # 检测内容中是否有句子大量重复
    sentences = [s.strip() for s in re.split(r'[.!?。！？\n]', text) if 10 < len(s.strip()) <= 200]
    if sentences:
        most_common = Counter(sentences).most_common(1)[0]
        repeat_ratio = most_common[1] / len(sentences)
        if repeat_ratio > 0.3:
            return True, f"句子大量重复（最频繁句子出现 {most_common[1]} 次，占比 {repeat_ratio:.1%}）"

    return False, ""


def main():
    execute = "--execute" in sys.argv

    cache = load_cache()
    items = cache.get("items", [])
    total = len(items)

    keep = []
    remove = []

    for item in items:
        low, reason = is_low_quality(item)
        if low:
            remove.append((item, reason))
        else:
            keep.append(item)

    print(f"缓存总数：{total}")
    print(f"将保留：{len(keep)}")
    print(f"将删除：{len(remove)}")
    print()

    # 按原因统计
    reason_counter = Counter(reason for _, reason in remove)
    print("【删除原因分布】")
    for reason, cnt in reason_counter.most_common():
        print(f"  {cnt:5d} 条 - {reason}")
    print()

    # 显示部分将被删除的 URL
    print("【将被删除的部分 URL 示例（前 20 条）】")
    for item, reason in remove[:20]:
        title = item.get("title", "")[:40]
        link = item.get("link", "")
        platform = item.get("source_platform", "")
        print(f"  [{platform}] {title}...\n    {link}\n    原因：{reason}")
    print()

    if execute:
        cache["items"] = keep
        save_cache(cache)
        print(f"已执行清理，缓存剩余 {len(keep)} 条。")
    else:
        print("这是预览模式，未实际删除。")
        print("如需执行删除，请运行：python clean_content_cache.py --execute")


if __name__ == "__main__":
    main()

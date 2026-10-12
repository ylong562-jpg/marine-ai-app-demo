# -*- coding: utf-8 -*-
"""
news_analysis 后端 - 公司关系提取服务

从新闻内容中提取船东与船厂之间的合作关系。
"""

import sys
import os

if __name__ == "__main__" and not getattr(sys, "frozen", False):
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

import json
import re
import traceback
from typing import Dict, List, Optional, Any

import dashscope
from http import HTTPStatus

from news_analysis.backend.common.crawler import crawl_url
from news_analysis.backend.common.config import is_vpn_needed


RELATIONSHIP_PROMPT = """你是一名航运行业信息分析专家。请仔细阅读下面这篇新闻，提取出文中明确提及的船东/公司与船厂之间的合作关系。

要求：
1. 如果新闻中只提到一家公司或无法判断两家公司关系，则返回空数组 []
2. 只提取明确存在合作关系的两家公司（shipowner 与 shipyard），不要推测
3. connection 只能是以下三种之一："造船订单"、"交船"、"维修改装"
4. date 请尽量从文中提取具体日期，格式为 YYYY-MM-DD；若只有年份或月份，请用 YYYY 或 YYYY-MM；若无法判断则留空
5. confidence_score 为 0-1 之间的浮点数，表示你对这条关系判断的置信度
6. source_url 请使用传入的 url 值
7. 返回严格合法的 JSON 数组，不要包含任何解释文字

输出格式示例：
[
  {
    "shipowner": "船东A公司",
    "shipyard": "船厂B公司",
    "connection": "造船订单",
    "date": "2026-03-15",
    "confidence_score": 0.92,
    "source_url": "https://example.com/news/1"
  }
]

新闻标题：{{TITLE}}
新闻链接：{{URL}}
新闻内容：
{{CONTENT}}
"""


def _call_qwen_for_relationships(prompt: str, model: str = "qwen-plus", timeout: int = 120) -> Optional[str]:
    """调用千问大模型进行关系提取，带硬超时。"""
    if not dashscope.api_key:
        raise RuntimeError("未设置 DASHSCOPE_API_KEY 环境变量")

    def _call():
        response = dashscope.Generation.call(
            model=model,
            messages=[
                {"role": "system", "content": "You are an expert in maritime industry relationship analysis. Always respond with valid JSON array format."},
                {"role": "user", "content": prompt},
            ],
            result_format="message",
            temperature=0.1,
            top_p=0.8,
        )

        if response.status_code == HTTPStatus.OK:
            return response["output"]["choices"][0]["message"]["content"]
        else:
            raise RuntimeError(f"API 调用失败: {response.code}")

    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            return future.result(timeout=timeout)
    except FutureTimeoutError:
        raise RuntimeError(f"关系提取 API 调用超时（{timeout}秒）")
    except Exception as e:
        raise RuntimeError(f"关系提取 API 异常: {e}")


def _parse_relationships_response(text: str, url: str, default_date: str = "") -> List[Dict[str, Any]]:
    """解析大模型返回的 JSON 数组。"""
    if not text:
        return []

    # 尝试从 markdown 代码块中提取 JSON
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1)

    text = text.strip()
    if not text:
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试修复末尾缺少的括号
        for suffix in ["", "]", "}]", "}}]"]:
            try:
                data = json.loads(text + suffix)
                break
            except json.JSONDecodeError:
                continue
        else:
            return []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    relationships = []
    for item in data:
        if not isinstance(item, dict):
            continue
        shipowner = str(item.get("shipowner", "")).strip()
        shipyard = str(item.get("shipyard", "")).strip()
        connection = str(item.get("connection", "")).strip()
        if not shipowner or not shipyard or not connection:
            continue

        # 规范化 connection
        connection_norm = connection
        for key in ["造船订单", "交船", "维修改装"]:
            if key in connection:
                connection_norm = key
                break
        if connection_norm not in {"造船订单", "交船", "维修改装"}:
            continue

        rel = {
            "shipowner": shipowner,
            "shipyard": shipyard,
            "connection": connection_norm,
            "date": str(item.get("date", default_date)).strip(),
            "confidence_score": item.get("confidence_score", ""),
            "source_url": str(item.get("source_url", url)).strip(),
        }
        relationships.append(rel)

    return relationships


def extract_relationships_from_url(
    url: str,
    title: str = "",
    source_platform: str = "",
    use_async: bool = False,
) -> List[Dict[str, Any]]:
    """
    从单个 URL 提取公司关系。

    Returns:
        关系字典列表
    """
    crawl_result = crawl_url(url, use_async=use_async)
    if not crawl_result.get("success"):
        raise RuntimeError(crawl_result.get("error") or "爬取失败")

    content = crawl_result.get("content", "")
    if not content:
        return []

    # 使用固定占位符替换，避免 content 中的 {xxx} 被 str.format 误解析为占位符
    prompt = RELATIONSHIP_PROMPT.replace("{{TITLE}}", title).replace("{{URL}}", url).replace("{{CONTENT}}", content[:8000])

    response_text = _call_qwen_for_relationships(prompt)
    relationships = _parse_relationships_response(response_text, url)

    # 补充来源平台、VPN 标记
    for rel in relationships:
        rel["source_platform"] = source_platform
        rel["needs_vpn"] = is_vpn_needed(source_platform)

    return relationships


def extract_relationships_from_news_links(
    news_items: List[Dict[str, Any]],
    use_async: bool = False,
    log_callback: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    批量从新闻链接中提取公司关系。

    Args:
        news_items: 新闻字典列表，每个字典需包含 link、title、source_platform
        use_async: 是否使用异步爬虫
        log_callback: 日志回调函数

    Returns:
        关系记录列表（每个关系一条记录）
    """
    log = log_callback or (lambda msg: None)
    results = []

    total = len(news_items)
    for i, news in enumerate(news_items, 1):
        url = news.get("link", "")
        title = news.get("title", "")
        platform = news.get("source_platform", "")

        log(f"[{i}/{total}] 处理: {title[:40]}... ({platform})")

        if not url:
            log("  跳过：无链接")
            continue

        try:
            relationships = extract_relationships_from_url(
                url=url,
                title=title,
                source_platform=platform,
                use_async=use_async,
            )
            results.extend(relationships)
            log(f"  成功提取 {len(relationships)} 条关系")
        except Exception as e:
            log(f"  失败: {str(e)}")

    return results

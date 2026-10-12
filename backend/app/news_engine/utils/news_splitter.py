# -*- coding: utf-8 -*-
"""
新闻按主维度拆分模块

当一条新闻的 tags 中同时包含多个【主维度】标签时，使用 AI 将新闻正文拆分为多段，
每段只围绕一个主维度，便于后续分别提取 entity/data 和生成 report。
"""

import sys
import os

if getattr(sys, 'frozen', False):
    current_dir = os.path.dirname(sys.executable)
    internal_dir = os.path.join(current_dir, '_internal')
    if os.path.exists(internal_dir):
        sys.path.insert(0, internal_dir)
    sys.path.insert(0, current_dir)
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import asyncio
from typing import List, Dict, Any
from ai_engine import BailianWebAnalyzer
from prompt import MAIN_DIMENSIONS


SPLIT_PROMPT_TEMPLATE = """你是一名专业的航运新闻编辑。请根据下面列出的【主维度】，把给定的新闻内容拆分为若干段，每段只围绕一个主维度展开。

给定主维度（按相关性从高到低）：
{main_dimensions}

拆分规则：
1. 每段内容必须只包含与该主维度直接相关、且能支撑其作为独立核心主题的句子。不要混入其他主维度的内容。
2. 如果某个主维度在新闻中只是附带提及、没有独立事实（例如"LNG双燃料"只是订单中的一个技术参数，不是独立的绿色航运主题），则**不要为其生成独立段落**，只需在相关内容所在的核心段落中作为背景一句带过即可。
3. 尽量保留原文表述，不要扩写、不要加入原文没有的信息。
4. 如果原文中某些句子同时涉及多个主维度，请将其归入最核心、最需要关注的主维度段落。
5. 输出必须且只能是一个合法的 JSON 数组，格式如下：
[
  {{"main_tag": "主维度名称1", "summary": "与该主维度相关的原文片段"}},
  {{"main_tag": "主维度名称2", "summary": "与该主维度相关的原文片段"}}
]

【格式强制要求】
- 必须返回合法 JSON 数组
- 不要输出任何解释、前言、后缀或 markdown 代码块标记
- 主维度名称必须严格来自上方给定列表
- 不要为没有独立事实的主维度生成段落

新闻标题：{title}
新闻正文：
{summary}
"""


def extract_main_dimensions_from_tags(tags: Any) -> List[str]:
    """
    从 tags 字段中解析出所有属于【主维度】的标签。
    tags 可以是 JSON 字符串或已解析的字典。
    """
    if not tags:
        return []

    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            return []

    if not isinstance(tags, dict):
        return []

    main_tags = tags.get("main_tags", [])
    if not isinstance(main_tags, list):
        return []

    main_dimensions_set = set(MAIN_DIMENSIONS)
    return [tag for tag in main_tags if tag in main_dimensions_set]


async def split_news_by_main_dimensions(
    news_item: Dict[str, Any],
    analyzer: BailianWebAnalyzer = None,
    max_tokens: int = 1500
) -> List[Dict[str, str]]:
    """
    根据新闻的主维度 tags 拆分新闻内容。

    返回：
        [{"main_tag": str, "summary": str}, ...]
    """
    main_dimensions = extract_main_dimensions_from_tags(news_item.get("tags"))

    if not main_dimensions:
        # 没有主维度时，整条新闻作为一段，主标签为空
        return [{"main_tag": "行业新闻", "summary": news_item.get("summary", "") or news_item.get("title", "")}]

    if len(main_dimensions) == 1:
        # 只有一个主维度，无需 AI 拆分
        return [{
            "main_tag": main_dimensions[0],
            "summary": news_item.get("summary", "") or news_item.get("title", "")
        }]

    if analyzer is None:
        analyzer = BailianWebAnalyzer(region="beijing")

    title = news_item.get("title", "")
    summary = news_item.get("summary", "") or ""
    # 控制长度，避免 token 超限
    if len(summary) > 6000:
        summary = summary[:6000]

    prompt = SPLIT_PROMPT_TEMPLATE.format(
        main_dimensions="、".join(main_dimensions),
        title=title,
        summary=summary
    )

    result = await analyzer.analyze(
        content=prompt,
        prompt="",
        max_tokens=max_tokens,
        model="qwen-plus"
    )

    if not result.get("success"):
        print(f"   ⚠️ AI 拆分失败: {result.get('error', '未知错误')}，回退到整段处理")
        return _fallback_split(main_dimensions, summary)

    raw = result.get("analysis", "")
    parsed = _parse_split_result(raw)

    if not parsed:
        return _fallback_split(main_dimensions, summary)

    # 校验并补齐缺失的主维度：仅对确实需要独立段落的主维度补齐
    found_tags = {item["main_tag"] for item in parsed}
    for dim in main_dimensions:
        if dim not in found_tags:
            # 如果该维度是高优先级维度，保留整段正文作为兜底
            parsed.append({"main_tag": dim, "summary": summary})

    # 过滤掉不在给定列表中的主维度
    parsed = [item for item in parsed if item["main_tag"] in set(main_dimensions)]

    return parsed


def _parse_split_result(raw: str) -> List[Dict[str, str]]:
    """解析 AI 返回的拆分结果，支持 JSON 数组或 markdown 代码块。"""
    if not raw:
        return []

    # 尝试提取 markdown 代码块
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(data, list):
        return []

    result = []
    for item in data:
        if isinstance(item, dict) and "main_tag" in item and "summary" in item:
            result.append({
                "main_tag": str(item["main_tag"]).strip(),
                "summary": str(item["summary"]).strip()
            })

    return result


def _fallback_split(main_dimensions: List[str], summary: str) -> List[Dict[str, str]]:
    """拆分失败时的兜底：每个主维度都返回完整 summary。"""
    return [{"main_tag": dim, "summary": summary} for dim in main_dimensions]


async def main():
    """简单测试"""
    sample = {
        "title": "测试新闻",
        "summary": "某船厂今日交付一艘 LNG 运输船。同时，该船厂还获得了一家欧洲船东的新造船订单。",
        "tags": json.dumps({
            "main_tags": ["船舶建造", "新造船订单"],
            "secondary_tags": ["亚洲船厂", "欧洲船东"]
        }, ensure_ascii=False)
    }
    result = await split_news_by_main_dimensions(sample)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

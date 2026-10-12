# -*- coding: utf-8 -*-
"""
news_analysis 后端 - 搜索策略加载器

从 strategies/ 目录加载策略配置，每个策略为一个 JSON 文件，包含：
- name: 策略名称（应与文件名一致）
- prompt: 用于新闻筛选的关键字/提示词
- processing_method: 文档处理方式说明
- return_limits: 返回内容限制说明
"""

import json
import os
from typing import Dict, List


DEFAULT_STRATEGIES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "strategies",
)


def load_strategies(strategies_dir: str = None) -> Dict[str, Dict[str, str]]:
    """
    加载指定目录下的所有策略文件。

    Returns:
        {策略名称: 策略配置字典}
    """
    directory = strategies_dir or DEFAULT_STRATEGIES_DIR
    strategies = {}

    if not os.path.isdir(directory):
        return strategies

    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(directory, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                config = json.load(f)
            name = config.get("name") or os.path.splitext(filename)[0]
            strategies[name] = config
        except Exception:
            continue

    return strategies


def get_strategy_names(strategies_dir: str = None) -> List[str]:
    """返回所有策略名称列表。"""
    return list(load_strategies(strategies_dir).keys())


def get_strategy(name: str, strategies_dir: str = None) -> Dict[str, str]:
    """根据名称获取策略配置，不存在时返回空字典。"""
    return load_strategies(strategies_dir).get(name, {})

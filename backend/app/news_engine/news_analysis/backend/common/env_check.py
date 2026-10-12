# -*- coding: utf-8 -*-
"""
news_analysis 后端 - 环境检查

检查运行船厂 AI 功能所需的环境和依赖。
"""

import os
from typing import List


REQUIRED_MODULES = ["mysql.connector", "openpyxl", "docx", "dashscope"]


def check_environment() -> List[str]:
    """
    检查环境配置，返回问题列表。空列表表示环境正常。
    """
    issues = []

    # 检查 DASHSCOPE_API_KEY
    if not os.getenv("DASHSCOPE_API_KEY"):
        issues.append("未设置 DASHSCOPE_API_KEY 环境变量")

    # 检查必要的库
    for module in REQUIRED_MODULES:
        try:
            __import__(module)
        except ImportError:
            issues.append(f"缺少 Python 库: {module}")

    return issues

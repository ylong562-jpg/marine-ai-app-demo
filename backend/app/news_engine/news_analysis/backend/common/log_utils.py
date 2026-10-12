# -*- coding: utf-8 -*-
"""
news_analysis 后端 - 日志捕获工具

用于将 CLI 模块的 print 输出捕获到 GUI 日志回调中。
"""

import io
import sys
from typing import Callable, Optional, Any


def run_with_log_capture(
    func: Callable,
    log_callback: Optional[Callable[[str], Any]] = None,
    *args,
    **kwargs
) -> Any:
    """
    执行函数并捕获其标准输出，逐行传给日志回调。

    Args:
        func: 要执行的函数
        log_callback: 日志回调函数，接收单行字符串
        *args, **kwargs: 传给 func 的参数

    Returns:
        func 的返回值
    """
    old_stdout = sys.stdout
    captured = io.StringIO()
    sys.stdout = captured
    try:
        result = func(*args, **kwargs)
    finally:
        sys.stdout = old_stdout

    if log_callback:
        output = captured.getvalue()
        if output:
            for line in output.splitlines():
                log_callback(line)
    return result

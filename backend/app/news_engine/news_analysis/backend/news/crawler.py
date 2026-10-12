# -*- coding: utf-8 -*-
"""
news_analysis 后端 - 新闻爬取与预览流程封装

将根目录 main.py / complete_news_pipeline.py 中的新闻爬取流程
封装为 GUI 可用的接口：
    - 爬取新闻并生成预览数据（不导入数据库）
    - 生成 HTML 预览文件
    - 将预览数据导入数据库

固定流程（无需选择）：
    爬取 -> AI 前置去重 -> AI 分类 -> 核心内容提取 -> 生成报告 -> 数据库历史去重 -> 预览
"""

import sys
import os
import contextlib
from typing import List, Dict, Any, Optional, Callable

# 确保项目根目录在路径中
_project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import asyncio

from news_analysis.backend.common.config import DEFAULT_DB_CONFIG, NEWS_TABLE

# 根目录流程模块
from complete_news_pipeline import (
    connect_to_database,
    ensure_table_schema,
    import_news_to_database,
    deduplicate_with_database,
    deduplicate_news_reports_with_database,
)
from news_combine import get_combined_news_with_metadata, clean_news_markdown, discover_crawlers
from ai_deduplicate_engine import ai_deduplicate_news
from ai_classify import re_classify
from ai_news_core_content_extraction import process_news_with_category_prompts
from ai_news_report_creating import generate_news_report_from_existing_data
from entity_normalizer import normalize_shipyard_entity
from content_extractor import extract_article
# 与 main.py 选项 1-4 对应的来源模式
SOURCE_MODES = {
    "no_vpn": {
        "label": "不需要VPN的网站新闻",
        "vpn_mode": "no_vpn",
        "include_wechat_mp": False,
    },
    "vpn": {
        "label": "需要VPN的新闻",
        "vpn_mode": "vpn",
        "include_wechat_mp": False,
    },
    "shipyard": {
        "label": "船厂网站新闻",
        "vpn_mode": "shipyard",
        "include_wechat_mp": False,
    },
    "wechat_only": {
        "label": "微信公众号文章",
        "vpn_mode": "wechat_only",
        "include_wechat_mp": True,
    },
}


def _log(msg: str, log_callback: Optional[Callable[[str], None]] = None):
    if log_callback:
        try:
            log_callback(msg)
        except Exception:
            print(msg)
    else:
        print(msg)


def _build_summary_from_news(news: Dict[str, Any]) -> str:
    """
    从新闻字典中构建 summary：核心有效字段为 summary，content 为历史冗余字段。

    逻辑：
        1. 优先使用已有的 summary
        2. 无 summary 时使用 news_report
        3. 都没有则返回空字符串
    """
    summary = news.get("summary", "")
    report = news.get("news_report", "")

    if summary:
        return summary
    if report:
        return report
    return ""


# 判定 summary 是否可用的阈值与截断标记
_SUMMARY_MIN_LENGTH = 80


def _is_summary_usable(summary: str) -> bool:
    """
    判断已有 summary 是否可直接用于重新生成。
    只要有内容且长度足够，即认为可用；不再因包含"阅读原文"等标记而回退爬取。
    """
    if not summary or not isinstance(summary, str):
        return False
    text = summary.strip()
    if not text:
        return False
    if len(text) < _SUMMARY_MIN_LENGTH:
        return False
    return True


@contextlib.contextmanager
def _patch_vpn_wait():
    """
    临时替换 news_combine.wait_for_vpn 为空操作。

    原因：GUI 模式下不再需要爬取前阻塞式确认 VPN；
    对于需要 VPN 的来源，我们会在获取完网页内容后再提示用户关闭 VPN。
    """
    import news_combine

    original_wait = news_combine.wait_for_vpn
    news_combine.wait_for_vpn = lambda: _log("[VPN] 已跳过爬取前 VPN 确认，继续执行...")
    try:
        yield
    finally:
        news_combine.wait_for_vpn = original_wait


# 缓存已发现的爬虫配置，避免 GUI 中反复扫描模块
_crawlers_cache = None


def _get_cached_crawlers():
    """获取已缓存的爬虫配置，首次调用时扫描。"""
    global _crawlers_cache
    if _crawlers_cache is None:
        _crawlers_cache = discover_crawlers()
    return _crawlers_cache


def get_available_modules_by_mode(mode: str) -> List[Dict[str, Any]]:
    """
    获取指定来源模式下可用的具体爬虫模块列表。

    Returns:
        List[Dict]: 每个元素包含 module（模块名）、platform（平台显示名）、enabled（默认启用）
    """
    config = SOURCE_MODES.get(mode)
    if not config:
        return []

    if mode == "wechat_only":
        # 微信公众号没有独立爬虫模块，由 WeRSS 统一获取
        return []

    no_vpn, vpn, shipyard = _get_cached_crawlers()

    if mode == "no_vpn":
        crawlers = no_vpn
    elif mode == "vpn":
        crawlers = vpn
    elif mode == "shipyard":
        crawlers = shipyard
    else:
        crawlers = []

    from confidence_config import get_module_platform
    result = []
    for c in crawlers:
        result.append({
            "module": c["module"],
            "platform": get_module_platform(c["module"]),
            "enabled": c.get("enabled", True),
        })
    return result


async def _pre_dedup_by_url_against_database(
    news_data: List[Dict[str, Any]],
    log_callback: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """
    与数据库中相同日期的新闻按 URL 进行预查重。

    逻辑：
        1. 连接数据库；
        2. 提取待入库新闻的所有 date（取前 10 位）；
        3. 查询这些日期下数据库中已有的 link；
        4. 若待入库新闻的 (date, link) 已存在，则视为重复并丢弃。

    Parameters:
        news_data: 待查重的新闻列表
        log_callback: 日志回调函数

    Returns:
        List[Dict]: 去除重复 URL 后的新闻列表
    """
    if not news_data:
        return news_data

    from news_analysis.backend.news.database import get_existing_url_date_pairs

    _log("[预查重] 连接数据库，准备按 URL 去重...", log_callback)
    conn = connect_to_database(
        host_name=DEFAULT_DB_CONFIG["host"],
        user_name=DEFAULT_DB_CONFIG["user"],
        port=DEFAULT_DB_CONFIG["port"],
        user_password=DEFAULT_DB_CONFIG["password"],
        db_name=DEFAULT_DB_CONFIG["database"],
    )
    if not conn:
        _log("[警告] 数据库连接失败，跳过 URL 预查重", log_callback)
        return news_data

    try:
        dates = {str(news.get("date", "")).strip()[:10] for news in news_data if news.get("date")}
        if not dates:
            _log("[预查重] 待查重新闻均无有效日期，跳过", log_callback)
            return news_data

        existing_pairs = get_existing_url_date_pairs(conn, dates, NEWS_TABLE)

        unique_news = []
        duplicate_count = 0
        seen_local_pairs = set()
        for news in news_data:
            date_str = str(news.get("date", "")).strip()[:10]
            link = news.get("link", "")
            pair = (date_str, link)

            if not link:
                unique_news.append(news)
                continue

            if pair in existing_pairs:
                duplicate_count += 1
                continue

            # 同时避免同一批次内相同 URL 重复
            if pair in seen_local_pairs:
                duplicate_count += 1
                continue
            seen_local_pairs.add(pair)
            unique_news.append(news)

        _log(
            f"[完成] URL 预查重：去除 {duplicate_count} 条重复新闻，剩余 {len(unique_news)} 条",
            log_callback,
        )
        return unique_news
    except Exception as e:
        _log(f"[警告] URL 预查重失败: {e}", log_callback)
        return news_data
    finally:
        conn.close()


async def run_crawl_preview_pipeline(
    source_mode: str = "no_vpn",
    selected_modules: Optional[List[str]] = None,
    limit: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    vpn_prompt_callback: Optional[Callable[[], bool]] = None,
) -> List[Dict[str, Any]]:
    """
    爬取新闻并生成可用于预览/导入的数据库记录。

    固定执行完整流程：
        1. 根据 source_mode 爬取指定来源新闻（可选只爬 selected_modules）
        2. 与数据库相同日期新闻进行 URL 级预查重
        3. AI 前置去重（防止多个网站重复报道同一事件）
        4. AI 重新分类
        5. 提取核心内容（entity/data 等）
        6. 生成 news_report
        7. 与数据库历史数据进行报告级去重（按主维度，同批次 + 近三天）
        8. 返回待导入的新闻列表（不含 id）

    Parameters:
        source_mode: 来源模式，对应 main.py 的 1-4 选项：no_vpn/vpn/shipyard/wechat_only
        selected_modules: 可选，指定只启用哪些爬虫模块（模块名列表）
        limit: 限制最终返回条数（None 表示不限制）
        start_date: 开始日期，YYYY-MM-DD，默认今天
        end_date: 结束日期，YYYY-MM-DD，默认与 start_date 相同
        log_callback: 日志回调函数
        vpn_prompt_callback: VPN 确认回调，返回 True 继续，False 取消

    Returns:
        List[Dict]: 待导入新闻列表
    """
    mode_config = SOURCE_MODES.get(source_mode)
    if not mode_config:
        raise ValueError(f"不支持的来源模式: {source_mode}")

    source_label = mode_config["label"]
    _log(f"[启动] 爬取预览流程：{source_label}", log_callback)
    if start_date or end_date:
        from news_combine import _normalize_date_range
        s, e = _normalize_date_range(start_date, end_date)
        date_desc = s if s == e else f"{s} 至 {e}"
        _log(f"[配置] 目标日期：{date_desc}", log_callback)
    if selected_modules:
        _log(f"[配置] 启用模块：{len(selected_modules)} 个", log_callback)
        _log(f"[配置] {', '.join(selected_modules)}", log_callback)
    else:
        _log("[配置] 启用全部模块", log_callback)

    # 微信公众号模式
    include_wechat_mp = mode_config["include_wechat_mp"]

    # 步骤1: 获取新闻数据（爬虫 + 网页正文）
    _log("[步骤1] 正在启动爬虫获取新闻数据，请耐心等待...", log_callback)
    try:
        with _patch_vpn_wait():
            news_data = await get_combined_news_with_metadata(
                vpn_mode=mode_config["vpn_mode"],
                fetch_content=True,
                include_wechat_mp=include_wechat_mp,
                enabled_modules=selected_modules,
                start_date=start_date,
                end_date=end_date,
            )
    except Exception as e:
        _log(f"[错误] 爬虫阶段发生异常：{e}", log_callback)
        return []

    if not news_data:
        _log("[错误] 没有获取到新闻数据", log_callback)
        return []

    with_summary = sum(1 for n in news_data if n.get("summary"))
    _log(f"[完成] 爬虫执行完成，共获取 {len(news_data)} 条新闻（{with_summary} 条包含摘要内容）", log_callback)

    # 步骤1b: 与数据库相同日期新闻按 URL 预查重
    news_data = await _pre_dedup_by_url_against_database(news_data, log_callback=log_callback)
    if not news_data:
        _log("[提示] 所有新闻均与数据库中相同日期 URL 重复", log_callback)
        return []

    # 针对需要 VPN 的来源：获取网页内容后提示用户关闭 VPN，再继续 AI 处理
    if source_mode in ("vpn", "shipyard"):
        _log("[VPN] 网页内容已获取，请关闭 VPN 后再继续 AI 处理", log_callback)
        if vpn_prompt_callback:
            if not vpn_prompt_callback():
                _log("[VPN] 用户取消，流程终止", log_callback)
                return []
            _log("[VPN] 用户确认已关闭 VPN，继续执行 AI 处理", log_callback)
        else:
            _log("[VPN] 未配置 VPN 确认回调，默认继续", log_callback)

    # 步骤3: AI前置去重（针对原始爬取新闻，防止同一事件被多个来源重复报道）
    if len(news_data) > 1:
        _log("[步骤3] AI前置去重...", log_callback)
        news_data = await ai_deduplicate_news(news_data, use_grouping=True)
        _log(f"[完成] 去重后剩余 {len(news_data)} 条新闻", log_callback)

    # 步骤4: AI重新分类
    _log("[步骤4] AI重新分类...", log_callback)
    news_data = await re_classify(news_data=news_data)
    if not news_data:
        _log("[错误] 重新分类失败", log_callback)
        return []
    _log(f"[完成] 重新分类完成，共 {len(news_data)} 条", log_callback)

    # 步骤5: 内容提取
    _log("[步骤5] 提取核心内容...", log_callback)
    news_data = await process_news_with_category_prompts(news_data)
    news_data = [n for n in news_data if n is not None]
    _log(f"[完成] 内容提取完成，剩余 {len(news_data)} 条有效新闻", log_callback)

    # 步骤5b: 船厂名称标准化
    _log("[步骤5b] 标准化船厂名称...", log_callback)
    for news in news_data:
        try:
            if news.get("entity"):
                news["entity"] = normalize_shipyard_entity(news["entity"])
            if news.get("data"):
                news["data"] = normalize_shipyard_entity(news["data"])
        except Exception as e:
            _log(f"[警告] 船厂名标准化失败: {e}", log_callback)
    _log("[完成] 船厂名称标准化完成", log_callback)

    # 步骤6: 生成新闻报告
    _log("[步骤6] 生成新闻报告...", log_callback)
    news_data = await generate_news_report_from_existing_data(news_data)
    _log("[完成] 报告生成完成", log_callback)

    # 步骤7: 数据库报告级去重
    if news_data:
        _log("[步骤7] 连接数据库并进行报告级历史去重...", log_callback)
        conn = connect_to_database(
            host_name=DEFAULT_DB_CONFIG["host"],
            user_name=DEFAULT_DB_CONFIG["user"],
            port=DEFAULT_DB_CONFIG["port"],
            user_password=DEFAULT_DB_CONFIG["password"],
            db_name=DEFAULT_DB_CONFIG["database"],
        )
        if not conn:
            _log("[错误] 数据库连接失败，跳过数据库去重", log_callback)
        else:
            try:
                await ensure_table_schema(conn)
                news_data = await deduplicate_news_reports_with_database(
                    news_data, conn, days_back=7, log_callback=log_callback
                )
                _log(f"[完成] 数据库去重完成，共 {len(news_data)} 条（重复项已标记）", log_callback)
            except Exception as e:
                _log(f"[警告] 数据库去重失败: {e}", log_callback)
            finally:
                conn.close()

    # 限制条数
    if limit and limit > 0 and len(news_data) > limit:
        news_data = news_data[:limit]
        _log(f"[限制] 取前 {limit} 条", log_callback)

    # 清洗字段：保留当前有效字段，subcategory/impact 已废弃
    db_columns = {
        "title",
        "link",
        "date",
        "summary",
        "entity",
        "data",
        "news_report",
        "tags",
        "source_platform",
        "confidence_score",
    }
    cleaned = []
    for news in news_data:
        item = {k: v for k, v in news.items() if k in db_columns}

        # summary 使用清洗后的完整原文（不再截断为 300 字摘要）
        if not item.get("summary"):
            item["summary"] = _build_summary_from_news(news)

        for field in ("entity", "data", "tags"):
            if field not in item or item[field] is None:
                item[field] = ""

        cleaned.append(item)

    _log(f"[完成] 预览数据准备完成：共 {len(cleaned)} 条待导入新闻", log_callback)
    return cleaned


async def run_database_import_pipeline(
    sql_params: Dict[str, Any],
    vpn_option: str,
    log_callback: Optional[Callable[[str], None]] = None,
    vpn_prompt_callback: Optional[Callable[[], bool]] = None,
    abort_callback: Optional[Callable[[], bool]] = None,
) -> List[Dict[str, Any]]:
    """
    从数据库加载已有新闻，重新获取原文后走完整 AI 流程。

    流程：
        1. 根据 sql_params 从数据库加载新闻
        2. 按 vpn_option（vpn / no_vpn）过滤
        3. 重新爬取每条新闻正文
        4. 提示用户关闭 VPN（与爬取流程保持一致）
        5. AI 重新分类 -> 提取核心内容 -> 标准化船厂名 -> 生成报告
        6. 返回带 _original_id 的预览数据，可用于覆盖原新闻

    Parameters:
        sql_params: 传给 fetch_news 的参数（date/keyword/limit 等）
        vpn_option: "vpn" 或 "no_vpn"
        log_callback: 日志回调
        vpn_prompt_callback: VPN 关闭确认回调
        abort_callback: 中止检查回调，返回 True 表示用户已请求中止

    Returns:
        List[Dict]: 处理后的新闻列表（带 _original_id）
    """
    if vpn_option not in ("vpn", "no_vpn"):
        raise ValueError("vpn_option 必须是 'vpn' 或 'no_vpn'")

    from news_analysis.backend.news.database import connect_db, fetch_news
    from news_analysis.backend.common.config import is_vpn_needed

    def _check_abort():
        if abort_callback and abort_callback():
            _log("[中止] 用户请求中止流程", log_callback)
            raise InterruptedError("用户中止")

    _check_abort()
    source_label = "需要VPN" if vpn_option == "vpn" else "无需VPN"
    _log(f"[启动] 从数据库导入新闻并处理，VPN 选项：{source_label}", log_callback)

    _log("[步骤0] 从数据库加载新闻...", log_callback)
    conn = connect_db()
    try:
        rows = fetch_news(conn, **sql_params)
    finally:
        conn.close()

    rows = [r for r in rows if r.get("link")]
    for r in rows:
        r["needs_vpn"] = is_vpn_needed(r.get("source_platform", ""))

    if vpn_option == "vpn":
        rows = [r for r in rows if r["needs_vpn"]]
    else:
        rows = [r for r in rows if not r["needs_vpn"]]

    if not rows:
        _log(f"[提示] 没有符合“{source_label}”条件的新闻", log_callback)
        return []

    _log(f"[完成] 加载 {len(rows)} 条新闻", log_callback)

    _check_abort()
    # 步骤1: 重新获取原文
    _log("[步骤1] 获取新闻原文...", log_callback)
    fetched = []
    for r in rows:
        _check_abort()
        url = r["link"]
        title = r.get("title", "")
        source_platform = r.get("source_platform", "")
        try:
            result = await extract_article(url=url, title=title, source_platform=source_platform)
            if result.get("success"):
                fetched.append({
                    "title": title,
                    "link": url,
                    "summary": result.get("content", ""),
                    "source_platform": source_platform,
                    "date": r.get("date"),
                    "_original_id": r.get("id"),
                    "_original_title": title,
                    "_original_link": url,
                    "_original_date": r.get("date"),
                })
                _log(f"[完成] 获取原文: {title[:40]}...", log_callback)
            else:
                _log(
                    f"[警告] 获取原文失败: {title[:40]}... - {result.get('error', '未知错误')}",
                    log_callback,
                )
        except Exception as e:
            _log(f"[错误] 获取原文异常: {title[:40]}... - {e}", log_callback)

    if not fetched:
        _log("[错误] 没有成功获取到任何新闻原文", log_callback)
        return []

    _log(f"[完成] 成功获取 {len(fetched)} 条新闻原文", log_callback)

    _check_abort()
    # 步骤2: 提示关闭 VPN
    _log("[VPN] 网页内容已获取，请关闭 VPN 后再继续 AI 处理", log_callback)
    if vpn_prompt_callback:
        if not vpn_prompt_callback():
            _log("[VPN] 用户取消，流程终止", log_callback)
            return []
        _log("[VPN] 用户确认已关闭 VPN，继续执行 AI 处理", log_callback)
    else:
        _log("[VPN] 未配置 VPN 确认回调，默认继续", log_callback)

    _check_abort()
    # 步骤3: AI 重新分类
    _log("[步骤3] AI重新分类...", log_callback)
    fetched = await re_classify(news_data=fetched)
    if not fetched:
        _log("[错误] 重新分类失败", log_callback)
        return []
    _log(f"[完成] 重新分类完成，共 {len(fetched)} 条", log_callback)

    _check_abort()
    # 步骤4: 提取核心内容
    _log("[步骤4] 提取核心内容...", log_callback)
    fetched = await process_news_with_category_prompts(fetched)
    fetched = [n for n in fetched if n is not None]
    _log(f"[完成] 内容提取完成，剩余 {len(fetched)} 条有效新闻", log_callback)

    _check_abort()
    # 步骤5: 船厂名称标准化
    _log("[步骤5] 标准化船厂名称...", log_callback)
    for news in fetched:
        try:
            if news.get("entity"):
                news["entity"] = normalize_shipyard_entity(news["entity"])
            if news.get("data"):
                news["data"] = normalize_shipyard_entity(news["data"])
        except Exception as e:
            _log(f"[警告] 船厂名标准化失败: {e}", log_callback)
    _log("[完成] 船厂名称标准化完成", log_callback)

    _check_abort()
    # 步骤6: 生成新闻报告
    _log("[步骤6] 生成新闻报告...", log_callback)
    fetched = await generate_news_report_from_existing_data(fetched)
    _log("[完成] 报告生成完成", log_callback)

    _check_abort()
    # 步骤6b: 报告级去重（同批次 + 近三天历史）
    if fetched:
        _log("[步骤6b] 连接数据库并进行报告级历史去重...", log_callback)
        conn = connect_to_database(
            host_name=DEFAULT_DB_CONFIG["host"],
            user_name=DEFAULT_DB_CONFIG["user"],
            port=DEFAULT_DB_CONFIG["port"],
            user_password=DEFAULT_DB_CONFIG["password"],
            db_name=DEFAULT_DB_CONFIG["database"],
        )
        if not conn:
            _log("[错误] 数据库连接失败，跳过报告级去重", log_callback)
        else:
            try:
                await ensure_table_schema(conn)
                fetched = await deduplicate_news_reports_with_database(
                    fetched, conn, days_back=7, log_callback=log_callback
                )
                _log(f"[完成] 报告级去重完成，共 {len(fetched)} 条（重复项已标记）", log_callback)
            except Exception as e:
                _log(f"[警告] 报告级去重失败: {e}", log_callback)
            finally:
                conn.close()

    # 清洗字段并保留原记录 id（category 已废弃）
    db_columns = {
        "title",
        "link",
        "date",
        "summary",
        "entity",
        "data",
        "news_report",
        "tags",
        "source_platform",
        "confidence_score",
    }
    cleaned = []
    for news in fetched:
        item = {k: v for k, v in news.items() if k in db_columns}

        if not item.get("date") and news.get("_original_date"):
            item["date"] = news["_original_date"]

        # summary 使用清洗后的完整原文（不再截断为 300 字摘要）
        if not item.get("summary"):
            item["summary"] = _build_summary_from_news(news)

        for field in ("entity", "data", "tags"):
            if field not in item or item[field] is None:
                item[field] = ""

        item["_original_id"] = news.get("_original_id")
        cleaned.append(item)

    _log(f"[完成] 数据库导入处理预览数据准备完成：共 {len(cleaned)} 条", log_callback)
    return cleaned


async def _import_async(
    news_list: List[Dict[str, Any]],
    log_callback: Optional[Callable[[str], None]] = None,
):
    _log("[步骤] 连接数据库...", log_callback)
    conn = connect_to_database(
        host_name=DEFAULT_DB_CONFIG["host"],
        user_name=DEFAULT_DB_CONFIG["user"],
        port=DEFAULT_DB_CONFIG["port"],
        user_password=DEFAULT_DB_CONFIG["password"],
        db_name=DEFAULT_DB_CONFIG["database"],
    )
    if not conn:
        raise ConnectionError("数据库连接失败")

    try:
        await ensure_table_schema(conn)
        _log("[步骤] 开始导入数据库...", log_callback)
        await import_news_to_database(conn, news_list)
    finally:
        conn.close()


def import_previewed_news(
    news_list: List[Dict[str, Any]],
    log_callback: Optional[Callable[[str], None]] = None,
):
    """
    将预览/编辑后的新闻列表导入数据库。
    """
    if not news_list:
        raise ValueError("没有可导入的新闻")

    asyncio.run(_import_async(news_list, log_callback))


async def regenerate_news_from_urls(
    news_items: List[Dict[str, Any]],
    log_callback: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """
    对已有新闻重新走完整 AI 处理流程。

    流程：
        1. 优先使用记录中的 summary 原文直接处理
        2. 仅当 summary 原文缺失/为空时，才回退到从 URL 爬取正文
        3. AI 重新分类
        4. 提取核心内容（entity/data 等）
        5. 生成 news_report
        6. 船厂名标准化
        7. 返回待覆盖的新闻列表（带 _original_id 标记原记录 id）

    Parameters:
        news_items: 已存在的新闻记录列表，每条至少包含 id、title、link、source_platform
        log_callback: 日志回调函数

    Returns:
        List[Dict]: 处理后可用于覆盖原记录的新闻列表
    """
    if not news_items:
        return []

    _log(f"[重新生成] 开始处理 {len(news_items)} 条新闻...", log_callback)

    # 步骤1: 优先使用 summary 原文；缺失时才爬取 URL
    _log("[步骤1] 优先使用 summary 原文，缺失时回退爬取 URL...", log_callback)
    regenerated = []
    for item in news_items:
        url = item.get("link", "")
        title = item.get("title", "")
        source_platform = item.get("source_platform", "")
        summary = item.get("summary", "")

        source_text = ""
        used_summary = False
        if _is_summary_usable(summary):
            source_text = str(summary).strip()
            used_summary = True
            _log(f"[完成] 使用 summary 原文: {title[:40]}...", log_callback)
        elif url:
            try:
                result = await extract_article(url=url, title=title, source_platform=source_platform)
                if result.get("success"):
                    source_text = result.get("content", "")
                    _log(f"[完成] 重新爬取: {title[:40]}...", log_callback)
                else:
                    _log(f"[警告] 爬取失败: {title[:40]}... - {result.get('error', '未知错误')}", log_callback)
            except Exception as e:
                _log(f"[错误] 爬取异常: {title[:40]}... - {e}", log_callback)
        else:
            _log(f"[警告] 记录 id={item.get('id')} 没有 summary 也没有 link，跳过", log_callback)
            continue

        if not source_text:
            if used_summary:
                _log(f"[警告] summary 为空，跳过: {title[:40]}...", log_callback)
            else:
                _log(f"[警告] 未能获取正文，跳过: {title[:40]}...", log_callback)
            continue

        # 重新生成时保留原标题和 URL，不因页面标题变化而改变
        regenerated.append({
            "title": title,
            "link": url,
            "summary": source_text,
            "source_platform": source_platform,
            "date": item.get("date"),
            "_original_id": item.get("id"),
            "_original_title": title,
            "_original_link": url,
            "_original_date": item.get("date"),
        })

    if not regenerated:
        _log("[错误] 没有成功获取到任何新闻正文", log_callback)
        return []

    _log(f"[完成] 成功获取 {len(regenerated)} 条新闻正文（含 summary 原文）", log_callback)

    # 步骤2: AI重新分类
    _log("[步骤2] AI重新分类...", log_callback)
    regenerated = await re_classify(news_data=regenerated)
    if not regenerated:
        _log("[错误] 重新分类失败", log_callback)
        return []
    _log(f"[完成] 重新分类完成，共 {len(regenerated)} 条", log_callback)

    # 步骤3: 提取核心内容
    _log("[步骤3] 提取核心内容...", log_callback)
    regenerated = await process_news_with_category_prompts(regenerated)
    regenerated = [n for n in regenerated if n is not None]
    _log(f"[完成] 内容提取完成，剩余 {len(regenerated)} 条有效新闻", log_callback)

    # 步骤4: 船厂名称标准化
    _log("[步骤4] 标准化船厂名称...", log_callback)
    for news in regenerated:
        try:
            if news.get("entity"):
                news["entity"] = normalize_shipyard_entity(news["entity"])
            if news.get("data"):
                news["data"] = normalize_shipyard_entity(news["data"])
        except Exception as e:
            _log(f"[警告] 船厂名标准化失败: {e}", log_callback)
    _log("[完成] 船厂名称标准化完成", log_callback)

    # 步骤5: 生成新闻报告
    _log("[步骤5] 生成新闻报告...", log_callback)
    regenerated = await generate_news_report_from_existing_data(regenerated)
    _log("[完成] 报告生成完成", log_callback)

    # 步骤5b: 报告级去重（同批次 + 近三天历史）
    # 重新生成是对指定新闻的再处理，目标是用新提取的内容覆盖原记录。
    # 如果在这里做历史去重，会把原记录自身（数据库中的旧版本）也算进“历史”，
    # 导致重新生成后的报道被自己的旧版本误判为重复而丢弃。
    # 因此重新生成流程跳过报告级历史去重；去重只在首次入库/导入流程中执行。
    _log("[步骤5b] 重新生成流程跳过报告级历史去重", log_callback)

    # 清洗字段并保留原记录 id（subcategory/impact/category 已废弃）
    db_columns = {
        "title",
        "link",
        "date",
        "summary",
        "entity",
        "data",
        "news_report",
        "tags",
        "source_platform",
        "confidence_score",
    }
    cleaned = []
    for news in regenerated:
        item = {k: v for k, v in news.items() if k in db_columns}

        # 保留原记录日期（若重新提取未获得日期）
        if not item.get("date") and news.get("_original_date"):
            item["date"] = news["_original_date"]

        # summary 兜底
        if not item.get("summary"):
            report = item.get("news_report", "")
            title = news.get("title", "")
            if report:
                item["summary"] = report[:300]
            elif title:
                item["summary"] = title
            else:
                item["summary"] = ""

        for field in ("entity", "data", "tags"):
            if field not in item or item[field] is None:
                item[field] = ""

        item["_original_id"] = news.get("_original_id")
        cleaned.append(item)

    _log(f"[完成] 重新生成数据准备完成：共 {len(cleaned)} 条", log_callback)
    return cleaned


async def _overwrite_async(
    news_list: List[Dict[str, Any]],
    log_callback: Optional[Callable[[str], None]] = None,
):
    """用重新生成的数据覆盖数据库中的原记录。"""
    _log("[步骤] 连接数据库...", log_callback)
    conn = connect_to_database(
        host_name=DEFAULT_DB_CONFIG["host"],
        user_name=DEFAULT_DB_CONFIG["user"],
        port=DEFAULT_DB_CONFIG["port"],
        user_password=DEFAULT_DB_CONFIG["password"],
        db_name=DEFAULT_DB_CONFIG["database"],
    )
    if not conn:
        raise ConnectionError("数据库连接失败")

    try:
        from news_analysis.backend.news.database import update_news_batch
        # 过滤掉没有 _original_id 的记录
        valid_news = [n for n in news_list if n.get("_original_id")]
        updated = update_news_batch(conn, valid_news)
        skipped = len(news_list) - len(valid_news)
        _log(f"[完成] 批量覆盖完成：更新 {updated} 条，跳过 {skipped} 条", log_callback)
    finally:
        conn.close()


def overwrite_news_in_database(
    news_list: List[Dict[str, Any]],
    log_callback: Optional[Callable[[str], None]] = None,
):
    """
    用预览/编辑后的重新生成数据覆盖数据库中的原记录。
    """
    if not news_list:
        raise ValueError("没有可覆盖的新闻")
    asyncio.run(_overwrite_async(news_list, log_callback))

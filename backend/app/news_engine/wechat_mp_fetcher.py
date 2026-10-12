# -*- coding: utf-8 -*-
"""
微信公众号文章获取模块
从 we-mp-rss 本地服务获取公众号文章，转换为与现有新闻系统兼容的格式

使用方式:
    from wechat_mp_fetcher import get_wechat_mp_news
    news = get_wechat_mp_news(base_url="http://localhost:8001", days_back=3, limit_per_feed=20)

自动刷新:
    如果配置了 ACCESS_KEY 和 SECRET_KEY，会在读取 RSS 前自动调用更新接口刷新所有公众号
"""

import sys
import urllib.request
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import time
import html
import re

from confidence_config import calculate_confidence

# 年份前缀检测正则：匹配 summary 开头是否已有 "【该新闻的日期是YYYY年"
_DATE_PREFIX_RE = re.compile(r'^【该新闻的日期是\d{4}年')


def _build_date_prefix(news_date):
    """根据新闻日期构建年份前缀说明。"""
    if not news_date:
        return ''
    pub_year_match = re.search(r'(\d{4})', str(news_date))
    if not pub_year_match:
        return ''
    pub_year = pub_year_match.group(1)
    return (
        f"【该新闻的日期是{pub_year}年，"
        f"如果每笔交易的订单信息没有明确的年份信息，默认为新闻日期的年份信息。】\n\n"
    )


def _add_date_prefix_to_summary(news_list):
    """在每条新闻的 summary 开头注入年份前缀（不修改正文其余部分）。"""
    for news in news_list:
        summary = news.get('summary', '')
        if not summary:
            continue
        prefix = _build_date_prefix(news.get('date', ''))
        if prefix and not _DATE_PREFIX_RE.match(str(summary)):
            news['summary'] = prefix + str(summary)
    return news_list

# Windows 终端 UTF-8 编码支持
# 注意：GUI 可能会重定向 sys.stdout（如替换为 _StdoutInterceptor），这类对象没有 buffer 属性，
# 因此需要先判断，避免在 GUI 中启动时直接崩溃。
if sys.platform == 'win32':
    import io
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name)
        if hasattr(stream, 'buffer'):
            try:
                wrapped = io.TextIOWrapper(
                    stream.buffer,
                    encoding='utf-8',
                    line_buffering=True
                )
                setattr(sys, stream_name, wrapped)
            except Exception:
                pass  # 包装失败时保持原 stream，避免崩溃

# === 用户配置的 Access Key / Secret Key ===
# 在 we-mp-rss 网页的"API Keys"页面创建，填入下方
ACCESS_KEY = "WK9WAVtGRcjhGhbaCE4TrSuOgVujgt-6od"
SECRET_KEY = "SKxsWhOGhRCxJ1QJ2eg7vs1xwn1gQg7OAa"

API_BASE = "/api/v1/wx"

# 微信公众号正文补全配置
# RSS 首次返回时，新文章可能尚未完成正文同步（content 为空或只有摘要），
# 低于此阈值则触发 we-mp-rss 单篇刷新接口并轮询等待。
CONTENT_MIN_LENGTH = 300
ARTICLE_REFRESH_TIMEOUT = 60      # 单篇刷新最长等待秒数
ARTICLE_REFRESH_POLL_INTERVAL = 3 # 轮询间隔秒数
ARTICLE_REFRESH_MAX_PER_FEED = 8  # 每个公众号最多补全篇数，避免整体流程过长

# 微信公众号名称别名表：把 WeRSS/we-mp-rss 中的实际订阅名统一成官方标准名，
# 避免大小写/简称差异导致置信度配置匹配失败。
WECHAT_NAME_ALIASES = {
    "sws外高桥造船": "SWS外高桥造船",
    "沪东中华": "沪东中华造船",
}


def fetch_url(url, timeout=30, auth_ak=None, auth_sk=None):
    """HTTP GET 请求，可选 AK/SK 认证"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    if auth_ak and auth_sk:
        headers['Authorization'] = f"AK-SK {auth_ak}:{auth_sk}"
    req = urllib.request.Request(url, headers=headers)
    # 对本地地址绕过系统代理，防止代理软件（Clash/V2Ray等）拦截 localhost 请求
    if 'localhost' in url or '127.0.0.1' in url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as response:
            return response.read().decode('utf-8')
    else:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode('utf-8')


def call_api(base_url, endpoint, auth_ak=None, auth_sk=None, timeout=30):
    """调用 we-mp-rss API（GET），返回 JSON"""
    url = f"{base_url}{API_BASE}{endpoint}"
    try:
        raw = fetch_url(url, timeout=timeout, auth_ak=auth_ak, auth_sk=auth_sk)
        data = json.loads(raw)
        # we-mp-rss 成功时返回 code=0 或 code=200
        if data.get("code") in (0, 200):
            return data.get("data", {})
        return {}
    except Exception as e:
        print(f"   ⚠️ API 调用失败 [{endpoint}]: {e}")
        return {}


def post_api(base_url, endpoint, auth_ak=None, auth_sk=None, timeout=30):
    """调用 we-mp-rss API（POST），返回 JSON"""
    url = f"{base_url}{API_BASE}{endpoint}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    if auth_ak and auth_sk:
        headers['Authorization'] = f"AK-SK {auth_ak}:{auth_sk}"
    req = urllib.request.Request(url, headers=headers, method='POST')
    try:
        if 'localhost' in url or '127.0.0.1' in url:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
        else:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
        if data.get("code") in (0, 200):
            return data.get("data", {})
        return {}
    except Exception as e:
        print(f"   ⚠️ API POST 调用失败 [{endpoint}]: {e}")
        return {}


def refresh_article_content(base_url, article_id, auth_ak=None, auth_sk=None):
    """调用 we-mp-rss 单篇文章刷新接口，返回 task_id"""
    try:
        task = post_api(base_url, f"/articles/{article_id}/refresh", auth_ak, auth_sk, timeout=30)
        if task and task.get("task_id"):
            return task.get("task_id")
        print(f"   ⚠️ 刷新文章 {article_id} 未返回任务ID")
    except Exception as e:
        print(f"   ⚠️ 刷新文章 {article_id} 异常: {e}")
    return None


def get_refresh_task_status(base_url, task_id, auth_ak=None, auth_sk=None):
    """查询文章刷新任务状态"""
    return call_api(base_url, f"/articles/refresh/tasks/{task_id}", auth_ak, auth_sk, timeout=10)


def wait_for_article_refresh(base_url, article_id, task_id, auth_ak=None, auth_sk=None,
                              timeout=ARTICLE_REFRESH_TIMEOUT):
    """轮询等待单篇文章刷新完成"""
    start = time.time()
    while time.time() - start < timeout:
        task = get_refresh_task_status(base_url, task_id, auth_ak, auth_sk)
        if not task:
            time.sleep(ARTICLE_REFRESH_POLL_INTERVAL)
            continue
        status = task.get("status")
        if status == "success":
            return True
        if status in ("failed", "error"):
            print(f"   ⚠️ 文章 {article_id} 刷新失败: {task.get('message', '未知错误')}")
            return False
        time.sleep(ARTICLE_REFRESH_POLL_INTERVAL)
    print(f"   ⚠️ 等待文章 {article_id} 刷新超时 ({timeout}s)")
    return False


def fetch_article_detail(base_url, article_id, auth_ak=None, auth_sk=None):
    """获取文章详情（含正文 content）"""
    try:
        detail = call_api(base_url, f"/articles/{article_id}?content=true", auth_ak, auth_sk, timeout=30)
        if detail:
            return detail
    except Exception as e:
        print(f"   ⚠️ 获取文章 {article_id} 详情异常: {e}")
    return None


def refresh_mp(base_url, mp_id, auth_ak, auth_sk):
    """调用更新接口刷新单个公众号"""
    endpoint = f"/mps/update/{mp_id}"
    url = f"{base_url}{API_BASE}{endpoint}"
    try:
        raw = fetch_url(url, timeout=60, auth_ak=auth_ak, auth_sk=auth_sk)
        data = json.loads(raw)
        code = data.get("code")
        if code in (0, 200):
            return True
        if code == 40402:
            # 频繁更新限制，属于正常防护，不算失败
            return "skipped"
        return False
    except Exception as e:
        print(f"   ⚠️ 刷新失败 [mp_id={mp_id}]: {e}")
        return False


def refresh_all_mps(base_url, auth_ak, auth_sk):
    """
    刷新所有已订阅公众号的文章
    1. 获取公众号列表
    2. 逐个调用更新接口
    3. 返回成功刷新的数量
    """
    if not auth_ak or not auth_sk:
        print("   ℹ️ 未配置 AK/SK，跳过自动刷新")
        return 0

    print(f"\n{'='*60}")
    print("🔄 自动刷新公众号文章")
    print(f"{'='*60}")

    # 获取公众号列表
    mps_data = call_api(base_url, "/mps?limit=100&offset=0", auth_ak, auth_sk)
    mps_list = mps_data.get("list", []) if isinstance(mps_data, dict) else []

    if not mps_list:
        print("   ⚠️ 没有可刷新的公众号")
        return 0

    print(f"   发现 {len(mps_list)} 个公众号，开始刷新...")
    success_count = 0

    for mp in mps_list:
        mp_id = mp.get("id")
        mp_name = mp.get("mp_name", mp.get("name", "未知"))
        if not mp_id:
            continue
        ok = refresh_mp(base_url, mp_id, auth_ak, auth_sk)
        if ok is True:
            print(f"   ✅ {mp_name}")
            success_count += 1
        elif ok == "skipped":
            print(f"   ⏭️ {mp_name} (距离上次更新不足6小时，已跳过)")
            success_count += 1  # 跳过也算成功（数据已较新）
        else:
            print(f"   ❌ {mp_name}")
        time.sleep(2)  # 每个公众号间隔2秒，降低请求频率

    print(f"\n   📊 刷新完成: {success_count}/{len(mps_list)} 个成功")
    print(f"{'='*60}")
    return success_count


def parse_rss_date(date_str):
    """解析 RSS pubDate 为 YYYY-MM-DD"""
    if not date_str:
        return datetime.now().strftime('%Y-%m-%d')

    formats = [
        '%a, %d %b %Y %H:%M:%S %z',
        '%a, %d %b %Y %H:%M:%S GMT',
        '%a, %d %b %Y %H:%M:%S +0800',
        '%a, %d %b %Y %H:%M:%S +0000',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d',
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue

    return datetime.now().strftime('%Y-%m-%d')


def strip_html(html_text):
    """去除 HTML 标签，保留纯文本"""
    if not html_text:
        return ''
    text = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def check_service_health(base_url="http://localhost:8001"):
    """检查 we-mp-rss 服务是否运行"""
    try:
        fetch_url(f"{base_url}/rss?limit=1&offset=0", timeout=5)
        return True
    except Exception as e:
        print(f"   ⚠️ 服务健康检查失败: {type(e).__name__}: {e}")
        return False


def get_feed_list(base_url="http://localhost:8001"):
    """获取已订阅的公众号列表"""
    try:
        rss_list_url = f"{base_url}/rss?limit=30&offset=0"
        xml = fetch_url(rss_list_url)
        root = ET.fromstring(xml)
        channel = root.find("channel")
        if channel is None:
            return []

        feeds = []
        for item in channel.findall("item"):
            t = item.find("title")
            l = item.find("link")
            g = item.find("guid")
            feed_id = None
            if g is not None and g.text and g.text.startswith("rss/"):
                feed_id = g.text.replace("rss/", "")
            elif l is not None and l.text and "/rss/" in l.text:
                feed_id = l.text.split("/rss/")[-1].split("?")[0]
            feeds.append({
                "name": t.text if t is not None else "未知公众号",
                "rss_url": l.text if l is not None else "",
                "feed_id": feed_id
            })
        return feeds
    except Exception as e:
        print(f"   ⚠️ 获取公众号列表失败: {e}")
        return []


def fetch_articles_from_feed(base_url, feed_id, limit=20):
    """获取单个公众号的文章列表"""
    try:
        url = f"{base_url}/feed/{feed_id}.xml?limit={limit}"
        xml = fetch_url(url)
        root = ET.fromstring(xml)
        channel = root.find("channel")
        if channel is None:
            return []

        articles = []
        for item in channel.findall("item"):
            title = item.find("title")
            link = item.find("link")
            pub = item.find("pubDate")
            desc = item.find("description")
            content = item.find(
                "content:encoded",
                {"content": "http://purl.org/rss/1.0/modules/content/"}
            )
            article_id_el = item.find("id")

            raw_content = ""
            if content is not None and content.text:
                raw_content = content.text
            elif desc is not None and desc.text:
                raw_content = desc.text

            articles.append({
                "article_id": article_id_el.text.strip() if article_id_el is not None and article_id_el.text else "",
                "title": title.text if title is not None else "",
                "link": link.text if link is not None else "",
                "pub_date": parse_rss_date(pub.text) if pub is not None else datetime.now().strftime('%Y-%m-%d'),
                "content": strip_html(raw_content),
                "raw_html": raw_content,
            })
        return articles
    except Exception as e:
        print(f"   ⚠️ 获取文章失败 [feed_id={feed_id}]: {e}")
        return []


def get_wechat_mp_news(base_url="http://localhost:8001", days_back=1, limit_per_feed=20, auto_refresh=True):
    """
    获取微信公众号文章，转换为与现有新闻系统兼容的格式

    Parameters:
        base_url: we-mp-rss 服务地址
        days_back: 只获取最近几天的文章
        limit_per_feed: 每个公众号获取的文章数量
        auto_refresh: 是否自动调用更新接口刷新公众号（需要配置 AK/SK）

    Returns:
        list: 与现有系统兼容的新闻列表
    """
    # 检查服务是否运行
    if not check_service_health(base_url):
        print(f"\n{'='*60}")
        print("⚠️  微信公众号文章获取跳过")
        print(f"{'='*60}")
        print(f"   we-mp-rss 服务未运行 ({base_url})")
        print("   如需获取公众号文章，请先启动服务:")
        print(r"   D:\git\...\news_ai\werss\start_werss.bat")
        print(f"{'='*60}")
        return []

    # 自动刷新所有公众号（如果配置了 AK/SK）
    if auto_refresh:
        refresh_all_mps(base_url, ACCESS_KEY, SECRET_KEY)

    # 获取公众号列表
    feeds = get_feed_list(base_url)
    if not feeds:
        print("   ⚠️ 没有订阅的公众号")
        return []

    print(f"\n{'='*60}")
    print("📱 获取微信公众号文章")
    print(f"{'='*60}")
    print(f"   发现 {len(feeds)} 个订阅的公众号")
    print(f"   获取最近 {days_back} 天的文章")

    # days_back=1 应该只包含今天，days_back=2 包含今天+昨天，以此类推
    effective_days = max(days_back - 1, 0)
    cutoff_date = (datetime.now() - timedelta(days=effective_days)).strftime('%Y-%m-%d')
    all_articles = []

    for feed in feeds:
        feed_name = WECHAT_NAME_ALIASES.get(feed["name"], feed["name"])
        feed_id = feed["feed_id"]

        if not feed_id:
            continue

        articles = fetch_articles_from_feed(base_url, feed_id, limit=limit_per_feed)

        # 过滤日期
        recent_articles = [a for a in articles if a["pub_date"] >= cutoff_date]

        # 对正文未同步完整的文章进行主动补全
        # we-mp-rss 刷新公众号后，新文章可能只有标题/链接，正文由后台任务异步抓取，
        # 这里对 content 过短的文章调用单篇刷新接口并轮询等待。
        if ACCESS_KEY and SECRET_KEY:
            incomplete_articles = [
                a for a in recent_articles
                if a.get('article_id') and len(a.get('content', '')) < CONTENT_MIN_LENGTH
            ]
            if incomplete_articles:
                print(f"   📝 发现 {len(incomplete_articles)} 条正文未完整同步，触发单篇刷新...")
            for idx, article in enumerate(incomplete_articles[:ARTICLE_REFRESH_MAX_PER_FEED], 1):
                article_id = article['article_id']
                total = min(len(incomplete_articles), ARTICLE_REFRESH_MAX_PER_FEED)
                print(f"   🔄 [{idx}/{total}] 补全正文: {article['title'][:40]}...")
                task_id = refresh_article_content(base_url, article_id, ACCESS_KEY, SECRET_KEY)
                if not task_id:
                    continue
                if wait_for_article_refresh(base_url, article_id, task_id, ACCESS_KEY, SECRET_KEY):
                    detail = fetch_article_detail(base_url, article_id, ACCESS_KEY, SECRET_KEY)
                    if detail and detail.get('content'):
                        article['content'] = strip_html(detail['content'])
                        article['raw_html'] = detail['content']
                        print(f"   ✅ 补全后正文 {len(article['content'])} 字符")
                    else:
                        print(f"   ⚠️ 补全后仍未获取到正文")
                time.sleep(1)

        for article in recent_articles:
            # 内容长度控制，防止 token 超限
            content = article['content']
            if len(content) > 30000:
                content = content[:30000] + "\n...[内容已截断]"

            # 转换为现有系统兼容的格式（保留原文，年份前缀在 AI 提取阶段注入）
            news_item = {
                'title': article['title'],
                'link': article['link'],
                'date': article['pub_date'],
                # 核心有效字段为 summary
                'summary': content,
                'source_platform': f'微信公众号:{feed_name}',
                'confidence_score': calculate_confidence(f'微信公众号:{feed_name}', 1, url=article.get('link', '')),
                'duplicate_count': 1,
                # 以下字段由后续 AI 处理填充
                'category': '',
                'entity': '',
                'data': '',
                'news_report': '',
            }
            all_articles.append(news_item)

        if recent_articles:
            print(f"   ✅ 微信公众号:{feed_name}: {len(recent_articles)} 条")
        else:
            print(f"   ℹ️  微信公众号:{feed_name}: 无新文章")

        time.sleep(0.5)  # 请求间隔，降低压力

    print(f"\n   📊 微信公众号总计: {len(all_articles)} 条")
    print(f"{'='*60}")

    # 给所有公众号文章注入年份前缀，确保 AI 提取日期时有明确锚点
    all_articles = _add_date_prefix_to_summary(all_articles)

    return all_articles


if __name__ == "__main__":
    # 测试
    result = get_wechat_mp_news(days_back=3, limit_per_feed=5)
    print(f"\n测试完成，获取 {len(result)} 条文章")
    for i, news in enumerate(result[:3], 1):
        print(f"\n{i}. {news['title'][:50]}...")
        print(f"   来源: {news['source_platform']}")
        print(f"   日期: {news['date']}")
        print(f"   链接: {news['link'][:60]}...")

# -*- coding: utf-8 -*-
"""
招商局工业集团/招商局船舶官网新闻爬虫
网址: https://www.cmindustry.com.hk/zh-CN/news/groupnews.html

该网站通过 JSON API 返回新闻数据：
https://www.cmindustry.com.hk/cmig/cms/data/pages/groupnews15/{page}.json

返回字段：
- articleId: 文章 ID
- articleTitle: 标题
- publishedDate: 发布时间戳（毫秒）
- articlePublisher: 发布者

链接拼接：固定前缀 + ?articleId= + articleId
"""

from datetime import datetime
import asyncio
import aiohttp


async def scrape_招商局船舶_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取招商局船舶官网新闻数据

    Parameters:
    -----------
    chrome_driver_path : str
        此参数保留用于接口统一，本爬虫不使用 Selenium
    headless : bool
        保留用于接口统一
    filter_today : bool
        是否只返回今天的新闻，默认为 False

    Returns:
    --------
    list: 新闻列表，每个元素为 dict，包含 title, link, date
    """
    base_url = 'https://www.cmindustry.com.hk'
    api_url = f'{base_url}/cmig/cms/data/pages/groupnews15/1.json'
    detail_prefix = f'{base_url}/zh-CN/news/groupNewsDetail.html'
    all_news = []
    seen_links = set()

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                data = await response.json()

        items = data.get('result', [])
        if not items:
            print(f"[招商局船舶] API 返回空数据")
            return []

        for item in items:
            article_id = item.get('articleId', '')
            title = item.get('articleTitle', '').strip()
            if not article_id or not title:
                continue

            # 拼接详情链接
            link = f'{detail_prefix}?articleId={article_id}'

            # 时间戳转日期（毫秒 → 秒）
            date_text = None
            timestamp_ms = item.get('publishedDate')
            if timestamp_ms:
                try:
                    dt = datetime.fromtimestamp(timestamp_ms / 1000)
                    date_text = dt.strftime('%Y-%m-%d')
                except Exception:
                    pass

            if link in seen_links:
                continue
            seen_links.add(link)

            all_news.append({
                'title': title,
                'link': link,
                'date': date_text,
            })

        # 日期筛选
        if filter_today:
            filtered = [n for n in all_news if n.get('date') == today_str]
            all_news = filtered

        print(f"[招商局船舶] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[招商局船舶] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    news = asyncio.run(scrape_招商局船舶_news(filter_today=False))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

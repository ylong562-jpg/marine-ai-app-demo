# -*- coding: utf-8 -*-
"""
福建省马尾造船股份有限公司官网新闻爬虫
网址: https://www.maweiship.com/news/news?pid=1

该网站为静态 HTML，新闻列表直接渲染在页面中：
<div class="list">
    <a class="item" href="/news/info?id=xxx">
        <div class="text-box">
            <div class="title">标题</div>
        </div>
        <div class="item-r">
            <div class="time">MM-DD</div>
            <div class="year">YYYY</div>
        </div>
    </a>
</div>
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import asyncio
import aiohttp


async def scrape_马尾造船_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取马尾造船官网新闻数据

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
    base_url = 'https://www.maweiship.com'
    news_url = f'{base_url}/news/news?pid=1'
    all_news = []
    seen_links = set()

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(news_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                html = await response.text()

        soup = BeautifulSoup(html, 'html.parser')

        # 新闻列表在 div.list 中
        news_container = soup.find('div', {'class': 'list'})
        if not news_container:
            print(f"[马尾造船] 未找到新闻列表容器")
            return []

        news_items = news_container.find_all('a', {'class': 'item'}, href=True)
        if not news_items:
            print(f"[马尾造船] 未找到新闻条目")
            return []

        for item in news_items:
            href = item.get('href', '').strip()
            if not href:
                continue

            if href.startswith('http'):
                link = href
            else:
                link = urljoin(base_url, href)

            # 提取标题
            title_elem = item.find('div', {'class': 'title'})
            title = title_elem.get_text(strip=True) if title_elem else ''

            # 提取日期：.time (MM-DD) + .year (YYYY)
            date_text = None
            time_elem = item.find('div', {'class': 'time'})
            year_elem = item.find('div', {'class': 'year'})
            if time_elem and year_elem:
                year = year_elem.get_text(strip=True)
                month_day = time_elem.get_text(strip=True)
                date_text = f"{year}-{month_day}"

            if not title or link in seen_links:
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

        print(f"[马尾造船] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[马尾造船] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    news = asyncio.run(scrape_马尾造船_news(filter_today=False))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

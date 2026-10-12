# -*- coding: utf-8 -*-
"""
江苏省镇江船厂集团有限公司官网新闻爬虫
网址: http://www.zjshipyard.com/news.php

该网站为静态 HTML，新闻列表直接渲染在页面中：
<div class="news">
    <ul>
        <li>
            <a href="news_show.php?id=xxx" title="标题">
                <span class="left">· 标题</span>
                <span class="right">YYYY-MM-DD</span>
            </a>
        </li>
    </ul>
</div>
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import asyncio
import aiohttp


async def scrape_江苏镇江_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取江苏镇江船厂官网新闻数据

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
    base_url = 'http://www.zjshipyard.com'
    news_url = f'{base_url}/news.php'
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

        # 新闻列表在 div.news > ul 中
        news_container = soup.find('div', {'class': 'news'})
        if not news_container:
            print(f"[江苏镇江] 未找到新闻列表容器")
            return []

        ul = news_container.find('ul')
        if not ul:
            print(f"[江苏镇江] 未找到新闻列表")
            return []

        news_items = ul.find_all('li')
        if not news_items:
            print(f"[江苏镇江] 未找到新闻条目")
            return []

        for item in news_items:
            link_elem = item.find('a', href=True)
            if not link_elem:
                continue

            href = link_elem.get('href', '').strip()
            if not href:
                continue

            if href.startswith('http'):
                link = href
            else:
                link = urljoin(base_url, href)

            # 提取标题：优先用 title 属性，其次用 left span
            title = link_elem.get('title', '').strip()
            if not title:
                left_elem = link_elem.find('span', {'class': 'left'})
                if left_elem:
                    title = left_elem.get_text(strip=True).lstrip('·').strip()

            # 提取日期
            date_text = None
            right_elem = link_elem.find('span', {'class': 'right'})
            if right_elem:
                date_text = right_elem.get_text(strip=True)

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

        print(f"[江苏镇江] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[江苏镇江] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    news = asyncio.run(scrape_江苏镇江_news(filter_today=False))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

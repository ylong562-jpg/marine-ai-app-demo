# -*- coding: utf-8 -*-
"""
常石集团（舟山）造船有限公司官网新闻爬虫
网址: https://www.tsuneishi.co.jp/tzs/chinese/news/

该网站为 WordPress 搭建，新闻列表直接渲染在 HTML 中：
<ul class="c-news_list">
    <li class="c-news_card">
        <div class="news_meta">
            <p class="date">YYYY.MM.DD</p>
        </div>
        <a href="..."><h2 class="c-ttl02">标题</h2></a>
    </li>
</ul>
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import asyncio
import aiohttp


async def scrape_常石集团_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取常石集团（舟山）造船官网新闻数据

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
    base_url = 'https://www.tsuneishi.co.jp'
    news_url = f'{base_url}/tzs/chinese/news/'
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

        # 新闻列表在 ul.c-news_list 中
        news_list = soup.find('ul', {'class': 'c-news_list'})
        if not news_list:
            print(f"[常石集团] 未找到新闻列表容器")
            return []

        # 遍历每个新闻条目
        for item in news_list.find_all('li', {'class': 'c-news_card'}):
            # 提取日期：在 .news_meta .date 中
            date_elem = item.find('p', {'class': 'date'})
            date_text = None
            if date_elem:
                raw_date = date_elem.get_text(strip=True)
                # 日期格式 YYYY.MM.DD → YYYY-MM-DD
                date_text = raw_date.replace('.', '-')

            # 提取标题和链接：在 h2.c-ttl02 的父级 a 标签中
            title_link = item.find('h2', {'class': 'c-ttl02'})
            if not title_link:
                continue

            link_elem = title_link.find_parent('a', href=True)
            if not link_elem:
                continue

            title = title_link.get_text(strip=True)
            href = link_elem.get('href', '').strip()
            if not href:
                continue

            if href.startswith('http'):
                link = href
            else:
                link = urljoin(base_url, href)

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

        print(f"[常石集团] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[常石集团] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    news = asyncio.run(scrape_常石集团_news(filter_today=False))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

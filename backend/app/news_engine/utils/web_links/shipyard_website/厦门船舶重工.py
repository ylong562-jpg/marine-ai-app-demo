# -*- coding: utf-8 -*-
"""
厦门船舶重工股份有限公司官网新闻爬虫
网址: https://www.xsi.com.cn/html/news.html

该网站为静态 HTML，新闻列表直接渲染在页面中，结构清晰：
<div class="row new-list">
    <div class="new-con">
        <a href="/html/news/xxxx.html">
            <h5>标题</h5>
            <span>YYYY-MM-DD</span>
        </a>
    </div>
</div>
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import asyncio
import aiohttp


async def scrape_厦门船舶重工_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取厦门船舶重工官网新闻数据

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
    base_url = 'https://www.xsi.com.cn'
    news_url = f'{base_url}/html/news.html'
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

        # 新闻列表在 .row.new-list 中
        news_list = soup.find('div', {'class': 'new-list'})
        if not news_list:
            print(f"[厦门船舶重工] 未找到新闻列表容器")
            return []

        # 遍历每个新闻条目
        for item in news_list.find_all('div', {'class': 'new-con'}):
            link_elem = item.find('a', href=True)
            if not link_elem:
                continue

            # 提取标题
            title_elem = link_elem.find('h5')
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)

            # 提取链接
            href = link_elem.get('href', '').strip()
            if not href or href.startswith('javascript') or href.startswith('#'):
                continue

            if href.startswith('http'):
                link = href
            else:
                link = urljoin(base_url, href)

            # 提取日期
            date_text = None
            span = link_elem.find('span')
            if span:
                date_text = span.get_text(strip=True)

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

        print(f"[厦门船舶重工] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[厦门船舶重工] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    news = asyncio.run(scrape_厦门船舶重工_news(filter_today=False))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

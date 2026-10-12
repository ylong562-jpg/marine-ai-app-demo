# -*- coding: utf-8 -*-
"""
舟山长宏国际船舶修造有限公司官网新闻爬虫
网址: https://www.zschship.com/news/3/

该网站为中企动力建站系统，新闻列表直接渲染在 HTML 中：
<div class="e_loop-17">
    <div class="cbox-17 p_loopitem">
        <p class="e_text-20 s_title"><a href="/News_details/xxx.html">标题</a></p>
        <p class="e_timeFormat-23 s_title">YYYY-MM-DD</p>
    </div>
</div>
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import asyncio
import aiohttp


async def scrape_舟山长宏_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取舟山长宏国际船舶修造官网新闻数据

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
    base_url = 'https://www.zschship.com'
    news_url = f'{base_url}/news/3/'
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

        # 新闻列表在 div.e_loop-17 中
        news_container = soup.find('div', {'class': 'e_loop-17'})
        if not news_container:
            print(f"[舟山长宏] 未找到新闻列表容器")
            return []

        # 遍历每个新闻条目
        for item in news_container.find_all('div', {'class': 'p_loopitem'}):
            # 提取标题和链接
            title_elem = item.find('p', {'class': 'e_text-20'})
            if not title_elem:
                continue

            link_elem = title_elem.find('a', href=True)
            if not link_elem:
                continue

            title = link_elem.get_text(strip=True)
            href = link_elem.get('href', '').strip()
            if not href:
                continue

            if href.startswith('http'):
                link = href
            else:
                link = urljoin(base_url, href)

            # 提取日期
            date_text = None
            date_elem = item.find('p', {'class': 'e_timeFormat-23'})
            if date_elem:
                date_text = date_elem.get_text(strip=True)

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

        print(f"[舟山长宏] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[舟山长宏] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    news = asyncio.run(scrape_舟山长宏_news(filter_today=False))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

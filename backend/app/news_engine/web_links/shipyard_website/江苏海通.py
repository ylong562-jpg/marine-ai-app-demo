# -*- coding: utf-8 -*-
"""
江苏海通海洋工程装备有限公司官网新闻爬虫
网址: https://www.nt-ship.com/news/4/

该网站为中企动力建站系统，新闻列表直接渲染在 HTML 中：
<div class="e_loop-1">
    <div class="cbox-1 p_loopitem">
        <div class="cbox-5-0 p_item">
            <p class="e_text-8 s_title"><a href="/news_details/xxx.html">标题</a></p>
            <P class="e_timeFormat-11 s_title">YYYY-MM-DD</P>
        </div>
    </div>
</div>
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import asyncio
import aiohttp


async def scrape_江苏海通_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取江苏海通海洋工程装备官网新闻数据

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
    base_url = 'https://www.nt-ship.com'
    news_url = f'{base_url}/news/4/'
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

        # 新闻列表在 div.e_loop-1 中
        news_container = soup.find('div', {'class': 'e_loop-1'})
        if not news_container:
            print(f"[江苏海通] 未找到新闻列表容器")
            return []

        news_items = news_container.find_all('div', {'class': 'p_loopitem'})
        if not news_items:
            print(f"[江苏海通] 未找到新闻条目")
            return []

        for item in news_items:
            # 标题和链接在 p.e_text-8 的 a 标签中
            title_elem = item.find('p', {'class': 'e_text-8'})
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

            # 日期在 e_timeFormat-11 中（完整格式 YYYY-MM-DD）
            date_text = None
            date_elem = item.find('p', {'class': 'e_timeFormat-11'})
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

        print(f"[江苏海通] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[江苏海通] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    news = asyncio.run(scrape_江苏海通_news(filter_today=False))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

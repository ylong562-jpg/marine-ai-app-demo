# -*- coding: utf-8 -*-
"""
泰州口岸船舶有限公司官网新闻爬虫
网址: https://www.cnkasc.com/sitecn/jyxx/list.html

该网站新闻列表通过 JavaScript 动态加载日期：
<ul class="list_news">
    <li class="a1">
        <span><script src="/sitecn/getData.aspx?nid=xxx"></script></span>
        <em>·</em>
        <a href="/sitecn/jyxx/xxx.html">标题</a>
    </li>
</ul>

日期需额外请求 getData.aspx?nid=xxx 获取，返回 document.write('YYYY-MM-DD');
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import asyncio
import aiohttp
import re


async def fetch_date(session, base_url, nid):
    """请求 getData.aspx 获取单条新闻的日期"""
    url = f"{base_url}/sitecn/getData.aspx?nid={nid}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            match = re.search(r"document\.write\('(.+?)'\)", text)
            if match:
                return match.group(1)
    except Exception:
        pass
    return None


async def scrape_泰州口岸船舶_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取泰州口岸船舶官网新闻数据

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
    base_url = 'https://www.cnkasc.com'
    news_url = f'{base_url}/sitecn/jyxx/list.html'
    all_news = []
    seen_links = set()

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            # 第一步：获取主页面
            async with session.get(news_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                html = await response.text()

            soup = BeautifulSoup(html, 'html.parser')

            news_container = soup.find('ul', {'class': 'list_news'})
            if not news_container:
                print(f"[泰州口岸船舶] 未找到新闻列表容器")
                return []

            news_items = news_container.find_all('li')
            if not news_items:
                print(f"[泰州口岸船舶] 未找到新闻条目")
                return []

            # 收集所有需要获取日期的 nid
            pending = []  # [(nid, title, link), ...]
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

                title = link_elem.get_text(strip=True)

                # 从 script src 中提取 nid
                nid = None
                script = item.find('script')
                if script:
                    src = script.get('src', '')
                    match = re.search(r'nid=(\d+)', src)
                    if match:
                        nid = match.group(1)

                if not title or not nid or link in seen_links:
                    continue
                seen_links.add(link)
                pending.append((nid, title, link))

            # 第二步：并发获取所有日期
            date_tasks = [fetch_date(session, base_url, nid) for nid, _, _ in pending]
            dates = await asyncio.gather(*date_tasks)

            for (nid, title, link), date_text in zip(pending, dates):
                all_news.append({
                    'title': title,
                    'link': link,
                    'date': date_text,
                })

        # 日期筛选
        if filter_today:
            filtered = [n for n in all_news if n.get('date') == today_str]
            all_news = filtered

        print(f"[泰州口岸船舶] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[泰州口岸船舶] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    news = asyncio.run(scrape_泰州口岸船舶_news(filter_today=False))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

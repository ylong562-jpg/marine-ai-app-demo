import os
# -*- coding: utf-8 -*-
"""
上海外高桥造船有限公司官网新闻爬虫
网址: https://www.chinasws.com/document/show/3.html

新闻中心页面包含：
- 公司新闻（焦点新闻 + 列表）
- 媒体报道（列表）
- 图片新闻（幻灯片）

列表中的日期格式为 MM-DD（无年份），需要根据当前日期推断年份。
"""

from bs4 import BeautifulSoup
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urljoin
from datetime import datetime
import asyncio
import time
import re


async def scrape_外高桥造船_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取上海外高桥造船官网新闻数据

    Parameters:
    -----------
    chrome_driver_path : str
        ChromeDriver 的路径
    headless : bool
        是否使用无头模式，默认为 True
    filter_today : bool
        是否只返回今天的新闻，默认为 False

    Returns:
    --------
    list: 新闻列表，每个元素为 dict，包含 title, link, date
    """
    chrome_options = Options()
    chrome_options.binary_location = os.getenv("CHROME_BIN", "/usr/lib64/chromium-browser/chromium-browser")
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    service = Service(executable_path=chrome_driver_path or os.getenv("CHROMEDRIVER_PATH", "/usr/lib64/chromium-browser/chromedriver"))
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(10)

    base_url = 'https://www.chinasws.com'
    news_url = f'{base_url}/document/show/3.html'
    all_news = []
    seen_links = set()  # 去重

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    current_year = today.year

    def infer_year(mm_dd: str) -> str:
        """根据 MM-DD 推断完整日期 YYYY-MM-DD"""
        try:
            month, day = map(int, mm_dd.split('-'))
            # 先用当前年份尝试
            candidate = datetime(current_year, month, day)
            # 如果日期在未来，则减一年
            if candidate.date() > today.date():
                candidate = datetime(current_year - 1, month, day)
            return candidate.strftime('%Y-%m-%d')
        except Exception:
            return None

    try:
        driver.get(news_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # === 1. 抓取焦点新闻 ===
        focus_news = soup.find('div', {'class': 'focusNews'})
        if focus_news:
            link_elem = focus_news.find('a', href=True)
            if link_elem:
                title = link_elem.get_text(strip=True)
                href = link_elem.get('href', '').strip()
                link = urljoin(base_url, href) if href else ''

                # 焦点新闻没有明确的日期标签，尝试从 <p> 摘要中提取 "X月X日"
                date_text = None
                p_elem = focus_news.find('p')
                if p_elem:
                    p_text = p_elem.get_text(strip=True)
                    match = re.search(r'(\d{1,2})月(\d{1,2})日', p_text)
                    if match:
                        month, day = match.groups()
                        mm_dd = f"{int(month):02d}-{int(day):02d}"
                        date_text = infer_year(mm_dd)

                if link and link not in seen_links:
                    seen_links.add(link)
                    all_news.append({
                        'title': title,
                        'link': link,
                        'date': date_text,
                    })

        # === 2. 抓取公司新闻列表 & 媒体报道列表 ===
        # 左侧区域包含两个列表：公司新闻（ul.docList）和 媒体报道（ul.docList.docList--row2）
        left_area = soup.find('div', {'class': 'leftArea'})
        if left_area:
            list_items = left_area.select('ul.docList li')
        else:
            list_items = soup.select('ul.docList li')

        for item in list_items:
            link_elem = item.find('a', href=True)
            if not link_elem:
                continue

            title = link_elem.get_text(strip=True)
            href = link_elem.get('href', '').strip()
            if not href:
                continue

            link = urljoin(base_url, href)

            # 提取日期（<time>MM-DD</time>）
            time_elem = item.find('time')
            date_text = None
            if time_elem:
                mm_dd = time_elem.get_text(strip=True)
                date_text = infer_year(mm_dd)

            if link in seen_links:
                continue
            seen_links.add(link)

            all_news.append({
                'title': title,
                'link': link,
                'date': date_text,
            })

        # === 3. 日期筛选 ===
        if filter_today:
            filtered = [n for n in all_news if n.get('date') == today_str]
            all_news = filtered

        print(f"[外高桥造船] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[外高桥造船] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_外高桥造船_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

import os
# -*- coding: utf-8 -*-
"""
山海关船舶重工有限责任公司官网新闻爬虫
网址: http://www.shgsic.com/xwzx/index.htm

新闻中心页面包含列表形式的新闻，每条新闻在一个 ul li 中。
日期格式为 [YYYY-MM-DD]，需要去除方括号和前导空格。
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


async def scrape_山海关船舶重工_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取山海关船舶重工官网新闻数据

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

    base_url = 'http://www.shgsic.com/xwzx'
    news_url = f'{base_url}/index.htm'
    all_news = []
    seen_links = set()  # 去重

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        driver.get(news_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # 新闻列表在 div.lbej 中
        list_container = soup.find('div', {'class': 'lbej'})
        if not list_container:
            print(f"[山海关船舶重工] 未找到新闻列表容器")
            return []

        for ul in list_container.find_all('ul'):
            for li in ul.find_all('li'):
                # 链接和标题
                link_elem = li.find('a', href=True)
                if not link_elem:
                    continue

                title = link_elem.get_text(strip=True)
                href = link_elem.get('href', '').strip()
                if not href:
                    continue

                link = urljoin(base_url + '/', href)

                # 日期在 li 中最后一个 span 中，格式为 "　[2024-01-15]"
                date_text = None
                spans = li.find_all('span')
                for span in spans:
                    span_text = span.get_text(strip=True)
                    match = re.search(r'\[(\d{4}-\d{2}-\d{2})\]', span_text)
                    if match:
                        date_text = match.group(1)
                        break

                if not title or link in seen_links:
                    continue
                seen_links.add(link)

                all_news.append({
                    'title': title,
                    'link': link,
                    'date': date_text,
                })

        # === 日期筛选 ===
        if filter_today:
            filtered = [n for n in all_news if n.get('date') == today_str]
            all_news = filtered

        print(f"[山海关船舶重工] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[山海关船舶重工] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_山海关船舶重工_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

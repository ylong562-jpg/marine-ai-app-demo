import os
# -*- coding: utf-8 -*-
"""
扬子江船业集团官网新闻爬虫
网址: https://www.yzjship.com/

爬取两个新闻栏目：
- 生产速递: /cn/news/24.html
- 经营接单: /cn/news/25.html

新闻列表位于 <ul class="newslist cf"> 中，
每条新闻包含标题、链接、日期和摘要。
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


async def scrape_扬子江船业_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取扬子江船业集团官网新闻数据

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

    base_url = 'https://www.yzjship.com'
    # 两个新闻栏目：生产速递 + 经营接单
    news_urls = [
        '/cn/news/24.html',
        '/cn/news/25.html',
    ]

    all_news = []
    today_str = datetime.now().strftime('%Y-%m-%d')

    try:
        for page_path in news_urls:
            page_url = urljoin(base_url, page_path)
            print(f"[扬子江船业] 正在获取: {page_url}")

            driver.get(page_url)
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(3)

            soup = BeautifulSoup(driver.page_source, 'html.parser')

            # 查找新闻列表
            news_list = soup.find('ul', {'class': 'newslist'})
            if not news_list:
                print(f"[扬子江船业] {page_path} 未找到新闻列表")
                continue

            news_items = news_list.find_all('li')
            page_count = 0

            for item in news_items:
                # 提取标题和链接
                title_link = item.find('div', {'class': 'txtarea'})
                if not title_link:
                    continue

                a_tag = title_link.find('h3')
                if not a_tag:
                    continue
                a_tag = a_tag.find('a', href=True)
                if not a_tag:
                    continue

                title = a_tag.get_text(strip=True)
                href = a_tag.get('href', '')

                # 处理相对链接，转换为完整链接
                if href.startswith('/'):
                    link = urljoin(base_url, href)
                elif href.startswith('http'):
                    link = href
                else:
                    link = urljoin(base_url, '/' + href)

                # 提取日期
                date_elem = title_link.find('p', {'class': 'date'})
                date_text = date_elem.get_text(strip=True) if date_elem else ''

                # 日期格式已经是 YYYY-MM-DD
                formatted_date = date_text if date_text else None

                # 构建新闻字典
                news_item = {
                    'title': title,
                    'link': link,
                    'date': formatted_date,
                }

                # 日期筛选逻辑 - 只保留今天的新闻（默认）
                if filter_today:
                    if not formatted_date:
                        continue
                    if formatted_date != today_str:
                        continue

                all_news.append(news_item)
                page_count += 1

            print(f"[扬子江船业] {page_path} 获取 {page_count} 条")

        print(f"[扬子江船业] 总计获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[扬子江船业] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_扬子江船业_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

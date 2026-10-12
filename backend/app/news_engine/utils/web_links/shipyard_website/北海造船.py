import os
# -*- coding: utf-8 -*-
"""
中国船舶集团青岛北海造船有限公司官网新闻爬虫
网址: http://www.bhshipyard.com.cn/

新闻列表位于首页的 "新闻中心" 板块，
每条新闻包含标题、链接和日期。
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


async def scrape_北海造船_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取北海造船官网新闻数据

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

    base_url = 'http://www.bhshipyard.com.cn'
    all_news = []

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        driver.get(base_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # 查找新闻中心的内容区域
        # 首页新闻中心在 "新闻中心" 标题后的 te-content 中
        news_blocks = soup.find_all('div', {'class': 'zjt'})
        news_container = None

        for block in news_blocks:
            title_div = block.find('div', {'class': 'zjt-title'})
            if title_div and '新闻中心' in title_div.get_text():
                news_container = block.find('div', {'class': 'te-content'})
                break

        if not news_container:
            print(f"[北海造船] 未找到新闻中心板块")
            return []

        news_list = news_container.find('ul')
        if not news_list:
            print(f"[北海造船] 新闻中心内未找到列表")
            return []

        news_items = news_list.find_all('li')

        for item in news_items:
            # 提取链接和标题
            link_elem = item.find('a', href=True)
            if not link_elem:
                continue

            title = link_elem.get_text(strip=True)
            href = link_elem.get('href', '')

            # 处理链接
            if href.startswith('http'):
                link = href
            elif href.startswith('/'):
                link = urljoin(base_url, href)
            else:
                link = urljoin(base_url, '/' + href)

            # 提取日期
            date_span = item.find('span')
            date_text = date_span.get_text(strip=True) if date_span else ''

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

        print(f"[北海造船] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[北海造船] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_北海造船_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

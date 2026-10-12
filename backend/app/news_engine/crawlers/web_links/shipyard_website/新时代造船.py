import os
# -*- coding: utf-8 -*-
"""
江苏新时代造船有限公司官网新闻爬虫
网址: http://www.ncship.com.cn/

新闻列表位于首页的 "资讯中心" 板块，
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
import re


async def scrape_新时代造船_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取江苏新时代造船有限公司官网新闻数据

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

    base_url = 'http://www.ncship.com.cn'
    all_news = []

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        driver.get(base_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # 查找资讯中心的新闻列表表格
        news_table = soup.find('table', {'class': 'MBlockTable'})
        if not news_table:
            print(f"[新时代造船] 未找到新闻列表表格")
            return []

        # 提取所有新闻行
        news_rows = news_table.find_all('tr')

        for row in news_rows:
            # 提取标题和链接
            title_link = row.find('a', href=True)
            if not title_link:
                continue

            title = title_link.get_text(strip=True)
            href = title_link.get('href', '')

            # 处理相对链接，转换为完整链接
            if href.startswith('/'):
                link = urljoin(base_url, href)
            elif href.startswith('http'):
                link = href
            else:
                link = urljoin(base_url, '/' + href)

            # 提取日期
            date_span = row.find('span')
            date_text = date_span.get_text(strip=True) if date_span else ''
            formatted_date = parse_shipyard_date(date_text)

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

        print(f"[新时代造船] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[新时代造船] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


def parse_shipyard_date(date_text):
    """
    将船厂网站的日期文本转换为 YYYY-MM-DD 格式

    支持的格式:
        '2026 / 6 / 1'  -> '2026-06-01'
        '2026 / 12 / 31' -> '2026-12-31'
    """
    if not date_text:
        return None

    date_text = date_text.strip()

    # 匹配 "YYYY / M / D" 或 "YYYY / MM / DD" 格式
    match = re.match(r'(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})', date_text)
    if match:
        year, month, day = match.groups()
        try:
            dt = datetime(int(year), int(month), int(day))
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            return None

    return None


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_新时代造船_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

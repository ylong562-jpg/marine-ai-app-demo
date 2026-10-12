import os
# -*- coding: utf-8 -*-
"""
南通象屿海洋装备股份有限公司官网新闻爬虫
网址: https://www.xysoe.com/news_1.html

新闻动态包含多个分类：
- news_1.html: 生产经营
- news_2.html: 管理动态
- news_3.html: 党群建设
- news_4.html: 文化引领

默认抓取生产经营分类（最相关），每条新闻以卡片形式展示。
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


async def scrape_象屿海装_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取南通象屿海洋装备官网新闻数据

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

    base_url = 'https://www.xysoe.com'
    news_url = f'{base_url}/news_1.html'
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

        # 新闻列表项直接为 a.list（网站改版后不再嵌套在特定容器中）
        news_items = soup.find_all('a', {'class': 'list'}, href=True)
        if not news_items:
            print(f"[象屿海装] 未找到新闻列表项")
            return []

        for item in news_items:
            href = item.get('href', '').strip()
            if not href:
                continue

            link = urljoin(base_url, href)

            # 标题在 .tit 中
            title_elem = item.find('div', {'class': 'tit'})
            title = title_elem.get_text(strip=True) if title_elem else ''

            # 日期在 .time 中
            date_elem = item.find('div', {'class': 'time'})
            date_text = date_elem.get_text(strip=True) if date_elem else None

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

        print(f"[象屿海装] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[象屿海装] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_象屿海装_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

import os
# -*- coding: utf-8 -*-
"""
黄海造船有限公司官网新闻爬虫
网址: http://zh.huanghaiship.com/news_list/1665547960976433152.html

新闻列表页面使用低代码平台构建，每条新闻以卡片形式展示，
包含封面图、标题、发布日期和详情链接。
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


async def scrape_黄海造船_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取黄海造船官网新闻数据

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

    base_url = 'http://zh.huanghaiship.com'
    news_url = f'{base_url}/news_list/1665547960976433152.html'
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

        # 新闻列表在 e_loop-1 中，每个条目是 p_loopitem
        loop_container = soup.find('div', {'class': 'e_loop-1'})
        if not loop_container:
            print(f"[黄海造船] 未找到新闻列表容器")
            return []

        loop_items = loop_container.find_all('div', {'class': 'p_loopitem'})

        for item in loop_items:
            # 标题和链接
            title_link = item.find('p', {'class': 'e_text-10'})
            if not title_link:
                continue

            link_elem = title_link.find('a', href=True)
            if not link_elem:
                continue

            title = link_elem.get_text(strip=True)
            href = link_elem.get('href', '').strip()
            if not href:
                continue

            link = urljoin(base_url, href)

            # 日期在 e_timeFormat-8 中
            date_elem = item.find('p', {'class': 'e_timeFormat-8'})
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

        print(f"[黄海造船] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[黄海造船] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_黄海造船_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

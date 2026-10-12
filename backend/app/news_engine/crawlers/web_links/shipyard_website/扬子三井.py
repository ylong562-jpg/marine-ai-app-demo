import os
# -*- coding: utf-8 -*-
"""
江苏扬子三井造船有限公司官网新闻爬虫
网址: https://yamicship.com/cn/news/34.html

新闻中心包含两个分类：
- /cn/news/34.html: 生产速递
- /cn/news/36.html: 船企新闻

每条新闻以卡片形式展示，包含标题、摘要、日期和详情链接。
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


async def scrape_扬子三井_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取扬子三井造船官网新闻数据

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

    base_url = 'https://yamicship.com'
    urls = [
        f'{base_url}/cn/news/34.html',  # 生产速递
        f'{base_url}/cn/news/36.html',  # 船企新闻
    ]
    all_news = []
    seen_links = set()  # 去重

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        for url in urls:
            driver.get(url)

            # 等待页面加载
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(3)

            soup = BeautifulSoup(driver.page_source, 'html.parser')

            # 新闻列表在 ul.newslist 中
            news_list = soup.find('ul', {'class': 'newslist'})
            if not news_list:
                continue

            for li in news_list.find_all('li'):
                # 标题在 h2.news_title 中
                title_elem = li.find('h2', {'class': 'news_title'})
                if not title_elem:
                    continue
                title = title_elem.get_text(strip=True)

                # 日期在 div.news_date 中
                date_elem = li.find('div', {'class': 'news_date'})
                date_text = date_elem.get_text(strip=True) if date_elem else None

                # 链接在 a.news_ckxq 或 pic 中的 a
                link_elem = li.find('a', {'class': 'news_ckxq'}, href=True)
                if not link_elem:
                    link_elem = li.find('a', href=True)
                if not link_elem:
                    continue

                href = link_elem.get('href', '').strip()
                if not href:
                    continue

                link = urljoin(base_url, href)

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

        print(f"[扬子三井造船] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[扬子三井造船] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_扬子三井_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

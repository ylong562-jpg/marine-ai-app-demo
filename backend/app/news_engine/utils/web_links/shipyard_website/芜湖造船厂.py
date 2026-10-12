import os
# -*- coding: utf-8 -*-
"""
芜湖造船厂有限公司官网新闻爬虫
网址: http://www.wuhu.com.cn/news.html

新闻中心页面包含：
- 顶部焦点新闻（news-view-box）
- 媒体列表（media-div 中的 media-items）
- 多页分页（共 61 页）

默认只抓取第 1 页（最新新闻），支持 filter_today 筛选。
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


async def scrape_芜湖造船厂_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取芜湖造船厂官网新闻数据

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

    base_url = 'http://www.wuhu.com.cn'
    news_url = f'{base_url}/news.html'
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

        # === 1. 抓取焦点新闻 ===
        focus_box = soup.find('div', {'class': 'news-view-box'})
        if focus_box:
            link_elem = focus_box.find('a', {'class': 'news-view-items'}, href=True)
            if link_elem:
                href = link_elem.get('href', '').strip()
                link = urljoin(base_url, href) if href else ''

                # 标题在 h2.sq_timer 中
                title_elem = link_elem.find('h2', {'class': 'sq_timer'})
                title = title_elem.get_text(strip=True) if title_elem else ''

                # 日期在 span 中
                date_span = link_elem.find('span')
                date_text = date_span.get_text(strip=True) if date_span else None

                if title and link and link not in seen_links:
                    seen_links.add(link)
                    all_news.append({
                        'title': title,
                        'link': link,
                        'date': date_text,
                    })

        # === 2. 抓取媒体列表 ===
        media_div = soup.find('div', {'class': 'media-div'})
        if media_div:
            media_items = media_div.find_all('a', {'class': 'media-items'}, href=True)
            for item in media_items:
                href = item.get('href', '').strip()
                if not href:
                    continue

                link = urljoin(base_url, href)

                # 标题在 h3 中
                title_elem = item.find('h3')
                title = title_elem.get_text(strip=True) if title_elem else ''

                # 日期在 span 中
                date_span = item.find('span')
                date_text = date_span.get_text(strip=True) if date_span else None

                if not title or link in seen_links:
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

        print(f"[芜湖造船厂] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[芜湖造船厂] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_芜湖造船厂_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

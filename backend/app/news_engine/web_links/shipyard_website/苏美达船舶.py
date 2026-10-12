import os
# -*- coding: utf-8 -*-
"""
苏美达船舶有限公司（新大洋造船）官网新闻爬虫
网址: https://www.sumecmarine.com/www/newscenter.html

新闻中心页面包含：
- 顶部焦点新闻（common-card）
- 新闻列表（tab1 下的多个 ul li）
- 分页共 12 页

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


async def scrape_苏美达船舶_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取苏美达船舶（新大洋造船）官网新闻数据

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

    base_url = 'https://www.sumecmarine.com'
    news_url = f'{base_url}/www/newscenter.html'
    all_news = []
    seen_links = set()  # 去重

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    def add_news(title, link, date_text):
        if not title or not link or link in seen_links:
            return
        seen_links.add(link)
        all_news.append({
            'title': title,
            'link': link,
            'date': date_text,
        })

    try:
        driver.get(news_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # === 1. 抓取焦点新闻（common-card）===
        focus_card = soup.find('div', {'class': 'common-card'})
        if focus_card:
            link_elem = focus_card.find('a', href=True)
            if link_elem:
                href = link_elem.get('href', '').strip()
                link = urljoin(base_url, href) if href else ''

                # 标题在 h3 中
                title_elem = link_elem.find('h3')
                title = title_elem.get_text(strip=True) if title_elem else ''

                # 焦点新闻没有明确日期标签，从链接路径或附近提取
                # 链接格式: /www/newscenter/20260416/37348.html
                date_text = None
                if href:
                    parts = href.split('/')
                    for part in parts:
                        if len(part) == 8 and part.isdigit():
                            date_text = f"{part[:4]}-{part[4:6]}-{part[6:8]}"
                            break

                add_news(title, link, date_text)

        # === 2. 抓取新闻列表 ===
        # 新闻在 tab1 下的多个 ul 中
        tab_container = soup.find('div', {'class': 'tab1'})
        if tab_container:
            news_lists = tab_container.find_all('ul')
            for ul in news_lists:
                for li in ul.find_all('li'):
                    # 日期在 <b> 中
                    date_elem = li.find('b')
                    date_text = date_elem.get_text(strip=True) if date_elem else None

                    # 标题在 h5 中
                    title_elem = li.find('h5')
                    if not title_elem:
                        continue
                    title = title_elem.get_text(strip=True)

                    # 链接在最后的 <a> 中
                    link_elem = li.find('a', href=True)
                    if not link_elem:
                        continue

                    href = link_elem.get('href', '').strip()
                    link = urljoin(base_url, href) if href else ''

                    add_news(title, link, date_text)

        # === 3. 日期筛选 ===
        if filter_today:
            filtered = [n for n in all_news if n.get('date') == today_str]
            all_news = filtered

        print(f"[苏美达船舶] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[苏美达船舶] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_苏美达船舶_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

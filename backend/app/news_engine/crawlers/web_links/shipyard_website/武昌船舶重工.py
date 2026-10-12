import os
# -*- coding: utf-8 -*-
"""
武昌船舶重工集团官网新闻爬虫
网址: http://www.wuchuan.com.cn/xwzx/jtxw/index.htm

新闻中心页面包含列表形式的新闻，每条新闻在一个 ul.news_ul li 中。
日期格式为 MM-DD（无年份），需要根据当前日期推断年份。
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


async def scrape_武昌船舶重工_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取武昌船舶重工集团官网新闻数据

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

    base_url = 'http://www.wuchuan.com.cn/xwzx/jtxw'
    news_url = f'{base_url}/index.htm'
    all_news = []
    seen_links = set()  # 去重

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    current_year = today.year

    def infer_year(mm_dd: str) -> str:
        """根据 MM-DD 推断完整日期 YYYY-MM-DD"""
        try:
            month, day = map(int, mm_dd.split('-'))
            candidate = datetime(current_year, month, day)
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

        # 新闻列表在 div.news_cont 中
        list_container = soup.find('div', {'class': 'news_cont'})
        if not list_container:
            print(f"[武昌船舶重工] 未找到新闻列表容器")
            return []

        for ul in list_container.find_all('ul', {'class': 'news_ul'}):
            for li in ul.find_all('li'):
                # 日期在 span 中（MM-DD）
                date_span = li.find('span')
                date_text = None
                if date_span:
                    mm_dd = date_span.get_text(strip=True)
                    date_text = infer_year(mm_dd)

                # 链接和标题
                link_elem = li.find('a', href=True)
                if not link_elem:
                    continue

                title = link_elem.get_text(strip=True)
                href = link_elem.get('href', '').strip()
                if not href:
                    continue

                # 链接可能是外部链接（如微信公众号）或相对路径
                if href.startswith('http'):
                    link = href
                else:
                    link = urljoin(base_url + '/', href)

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

        print(f"[武昌船舶重工] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[武昌船舶重工] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_武昌船舶重工_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

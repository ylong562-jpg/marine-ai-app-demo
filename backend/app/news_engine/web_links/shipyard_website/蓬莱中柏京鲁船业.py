import os
# -*- coding: utf-8 -*-
"""
蓬莱中柏京鲁船业有限公司官网新闻爬虫
网址: https://www.jinglushipyard.com/list-14-1.html

新闻中心页面包含：
- 推荐新闻（gsxwtj）：3 条带图片的置顶新闻
- 全部新闻（gsqbxw）：列表形式的新闻，含分页

日期格式：推荐新闻为 YYYY-MM-DD；全部新闻需从 .gsyue(YYYY-MM) 和 .gsri(DD) 组合。
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


async def scrape_蓬莱中柏京鲁船业_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取蓬莱中柏京鲁船业官网新闻数据

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

    base_url = 'https://www.jinglushipyard.com'
    news_url = f'{base_url}/list-14-1.html'
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

        # === 1. 抓取推荐新闻（gsxwtj）===
        tj_div = soup.find('div', {'class': 'gsxwtj'})
        if tj_div:
            for li in tj_div.find_all('li'):
                link_elem = li.find('a', href=True)
                if not link_elem:
                    continue

                href = link_elem.get('href', '').strip()
                link = urljoin(base_url, href) if href else ''

                # 标题在 a 的文本中（br 之前的部分）
                title = link_elem.get_text(strip=True).split('京鲁船业')[0] if '京鲁船业' in link_elem.get_text() else link_elem.get_text(strip=True)
                # 更精确的提取：去掉日期 span 的内容
                date_span = link_elem.find('span')
                if date_span:
                    # 复制一个副本用于提取标题
                    for span in link_elem.find_all('span'):
                        span.extract()
                    title = link_elem.get_text(strip=True)
                    # 恢复（不需要，因为已经解析了）

                date_text = date_span.get_text(strip=True) if date_span else None

                add_news(title, link, date_text)

        # === 2. 抓取全部新闻（gsqbxw）===
        qb_div = soup.find('div', {'class': 'gsqbxw'})
        if qb_div:
            for li in qb_div.find_all('li'):
                # 标题在 .gsqbbt a 中
                title_div = li.find('div', {'class': 'gsqbbt'})
                if not title_div:
                    continue

                link_elem = title_div.find('a', href=True)
                if not link_elem:
                    continue

                title = link_elem.get_text(strip=True)
                href = link_elem.get('href', '').strip()
                link = urljoin(base_url, href) if href else ''

                # 日期从 .gsyue 和 .gsri 组合
                yue_elem = li.find('div', {'class': 'gsyue'})
                ri_elem = li.find('div', {'class': 'gsri'})
                date_text = None
                if yue_elem and ri_elem:
                    yue_text = yue_elem.get_text(strip=True)  # 2026-03
                    ri_text = ri_elem.get_text(strip=True)    # 21
                    date_text = f"{yue_text}-{ri_text.zfill(2)}"

                add_news(title, link, date_text)

        # === 3. 日期筛选 ===
        if filter_today:
            filtered = [n for n in all_news if n.get('date') == today_str]
            all_news = filtered

        print(f"[蓬莱中柏京鲁船业] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[蓬莱中柏京鲁船业] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_蓬莱中柏京鲁船业_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

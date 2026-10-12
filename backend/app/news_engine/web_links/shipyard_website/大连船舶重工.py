import os
# -*- coding: utf-8 -*-
"""
大连船舶重工集团有限公司官网新闻爬虫
网址: http://www.dsic.cn/

首页"新闻中心"板块包含两个标签页：
- 集团新闻（默认显示）
- 媒体聚焦

每条新闻在表格行中，包含标题、链接和日期。
<a> 标签的 title 属性包含完整标题，文本节点可能被截断，优先使用 title。
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


async def scrape_大连船舶重工_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取大连船舶重工官网新闻数据

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

    base_url = 'http://www.dsic.cn'
    all_news = []

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        driver.get(base_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # 查找新闻中心板块（id="tab" 的 div）
        tab_div = soup.find('div', {'id': 'tab'})
        if not tab_div:
            print(f"[大连船舶重工] 未找到新闻中心板块")
            return []

        # 两个 tab 的内容分别在两个 div 中（第一个有 class="block"，第二个没有或有空 class）
        content_divs = tab_div.find_all('div', recursive=False)
        if not content_divs:
            # 备选：查找所有直接子 div
            content_divs = [d for d in tab_div.children if hasattr(d, 'name') and d.name == 'div']

        for div in content_divs:
            # 查找其中的表格行
            table = div.find('table')
            if not table:
                continue

            tbody = table.find('tbody')
            if tbody:
                rows = tbody.find_all('tr')
            else:
                rows = table.find_all('tr')

            for row in rows:
                # 每行包含：· 标记、标题链接、日期
                link_elem = row.find('a', href=True)
                if not link_elem:
                    continue

                href = link_elem.get('href', '').strip()
                if not href:
                    continue

                # 优先使用 title 属性（完整标题），否则使用文本内容
                title = link_elem.get('title', '').strip()
                if not title:
                    title = link_elem.get_text(strip=True)

                # 处理链接
                if href.startswith('http'):
                    link = href
                elif href.startswith('/'):
                    link = urljoin(base_url, href)
                else:
                    link = urljoin(base_url, '/' + href)

                # 提取日期（在第三个 td 中的 span.xz）
                date_span = row.find('span', {'class': 'xz'})
                # 注意：第一个 td 的 class 也是 xz（·符号），所以要过滤掉非日期内容
                date_text = ''
                if date_span:
                    date_text = date_span.get_text(strip=True)
                    # 日期格式是 YYYY-MM-DD，如果不是则尝试其他 span
                    if not (len(date_text) == 10 and date_text[4] == '-' and date_text[7] == '-'):
                        # 查找所有 xz class 的 span，找符合日期格式的
                        all_spans = row.find_all('span', {'class': 'xz'})
                        for sp in all_spans:
                            sp_text = sp.get_text(strip=True)
                            if len(sp_text) == 10 and sp_text[4] == '-' and sp_text[7] == '-':
                                date_text = sp_text
                                break

                formatted_date = date_text if date_text else None

                news_item = {
                    'title': title,
                    'link': link,
                    'date': formatted_date,
                }

                # 日期筛选逻辑
                if filter_today:
                    if not formatted_date:
                        continue
                    if formatted_date != today_str:
                        continue

                all_news.append(news_item)

        print(f"[大连船舶重工] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[大连船舶重工] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_大连船舶重工_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

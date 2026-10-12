import os
# -*- coding: utf-8 -*-
"""
大连中远海运川崎船舶工程有限公司官网新闻爬虫
网址: https://www.dacks.com.cn/col/col15580/index.html

该网站使用 JCMS 内容管理系统，新闻列表通过 JavaScript 动态加载到
<div class="neirong_right_nr"> 容器中。页面包含 TSPD 反爬虫机制，
需要足够等待时间让脚本执行完毕。
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


async def scrape_大连中远海运川崎船舶_news(chrome_driver_path=None, headless=True, filter_today=False):
    """
    爬取大连中远海运川崎船舶官网新闻数据

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

    base_url = 'https://www.dacks.com.cn'
    news_url = f'{base_url}/col/col15580/index.html'
    all_news = []
    seen_links = set()

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    try:
        driver.get(news_url)

        # 等待页面基础结构加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        # JCMS + TSPD 需要较长时间执行 JS
        time.sleep(8)

        # 等待动态内容容器出现并包含内容
        try:
            WebDriverWait(driver, 15).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, ".neirong_right_nr a")) > 0
            )
        except Exception:
            pass

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # 新闻内容在 .neirong_right_nr 中
        content_div = soup.find('div', {'class': 'neirong_right_nr'})
        if not content_div:
            print(f"[大连中远海运川崎] 未找到新闻内容容器")
            return []

        # 尝试多种常见列表结构
        news_items = []

        # 模式 1: ul > li > a
        for li in content_div.find_all('li'):
            link_elem = li.find('a', href=True)
            if link_elem:
                news_items.append(li)

        # 模式 2: div.item / div.list-item
        if not news_items:
            for item in content_div.find_all(['div', 'p'], class_=re.compile(r'item|list|news')):
                link_elem = item.find('a', href=True)
                if link_elem:
                    news_items.append(item)

        # 模式 3: 表格行
        if not news_items:
            table = content_div.find('table')
            if table:
                for tr in table.find_all('tr'):
                    link_elem = tr.find('a', href=True)
                    if link_elem:
                        news_items.append(tr)

        # 模式 4: 直接查找所有带 href 的 a 标签
        if not news_items:
            link_elems = content_div.find_all('a', href=True)
            for link_elem in link_elems:
                href = link_elem.get('href', '')
                if href and not href.startswith('javascript') and not href.startswith('#'):
                    news_items.append(link_elem)

        for item in news_items:
            # 如果是直接查到的 a 标签
            if item.name == 'a':
                link_elem = item
                parent = item.find_parent()
            else:
                link_elem = item.find('a', href=True)
                parent = item

            if not link_elem:
                continue

            title = link_elem.get_text(strip=True)
            href = link_elem.get('href', '').strip()
            if not href or href.startswith('javascript') or href.startswith('#'):
                continue

            # 处理链接
            if href.startswith('http'):
                link = href
            else:
                link = urljoin(base_url, href)

            # 提取日期：在兄弟节点或父节点中查找日期模式
            date_text = None
            item_str = str(parent)
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', item_str)
            if date_match:
                date_text = date_match.group(1)
            else:
                # 尝试 YY-MM-DD
                date_match = re.search(r'(\d{2}-\d{2}-\d{2})', item_str)
                if date_match:
                    yy_mm_dd = date_match.group(1)
                    try:
                        yy = int(yy_mm_dd[:2])
                        current_yy = today.year % 100
                        year = 2000 + yy if yy <= current_yy + 1 else 1900 + yy
                        date_text = f"{year}-{yy_mm_dd[3:5]}-{yy_mm_dd[6:8]}"
                    except Exception:
                        pass

            if not title or link in seen_links:
                continue
            seen_links.add(link)

            all_news.append({
                'title': title,
                'link': link,
                'date': date_text,
            })

        # 日期筛选
        if filter_today:
            filtered = [n for n in all_news if n.get('date') == today_str]
            all_news = filtered

        print(f"[大连中远海运川崎] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[大连中远海运川崎] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    news = asyncio.run(scrape_大连中远海运川崎船舶_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True
    ))

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

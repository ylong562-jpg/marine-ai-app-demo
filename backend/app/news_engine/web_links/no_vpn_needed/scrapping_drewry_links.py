import sys
import os

# 添加父目录到路径，支持直接运行
_current_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_current_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from bs4 import BeautifulSoup

from selenium.webdriver.chrome.service import Service

from selenium.webdriver.chrome.options import Options

from selenium import webdriver

from selenium.webdriver.common.by import By

from selenium.webdriver.support.ui import WebDriverWait

from selenium.webdriver.support import expected_conditions as EC

from urllib.parse import urljoin

from datetime import datetime, timedelta

import asyncio

import time

import re

from date_utils import get_today_str, get_yesterday_str, parse_date, is_today_or_recent





def parse_drewry_date(date_text):

    """

    �?Drewry 日期文本中提取日�?
    例如: '20 Mar 2026: Drewry's Intra-Asia Container Index...' -> '2026-03-20'

    """

    if not date_text:

        return None

    

    # 匹配日期格式: "DD MMM YYYY"

    match = re.match(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})', date_text)

    if match:

        day, month_str, year = match.groups()

        months = {

            'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,

            'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12

        }

        month = months.get(month_str, 1)

        return f"{year}-{month:02d}-{int(day):02d}"

    

    return None





async def scrape_drewry_news(chrome_driver_path, headless=True, filter_today=True, target_date=None, max_news=None):

    """

    爬取Drewry Shipping Consultants网站新闻数据



    Parameters:

    chrome_driver_path (str): chromedriver的完整路�?
    headless (bool): 是否使用无头模式，默认为True

    filter_today (bool): 是否筛选今日新闻，默认为True

    target_date (str): 目标日期，格式为'YYYY-MM-DD'，默认为None



    Returns:

    list: 包含所有新闻字典的大列表，每个字典包含'title', 'link', 'date', 'category'四个�?
    """

    # 设置Chrome选项

    chrome_options = Options()

    chrome_options.binary_location = os.getenv("CHROME_BIN", "/usr/lib64/chromium-browser/chromium-browser")
    if headless:

        chrome_options.add_argument("--headless")

    chrome_options.add_argument("--no-sandbox")

    chrome_options.add_argument("--disable-dev-shm-usage")

    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

    chrome_options.add_argument(

        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")



    # 初始化浏览器驱动

    service = Service(executable_path=chrome_driver_path or os.getenv("CHROMEDRIVER_PATH", "/usr/lib64/chromium-browser/chromedriver"))

    driver = webdriver.Chrome(service=service, options=chrome_options)

    driver.implicitly_wait(10)



    # 定义基础URL

    base_url = 'https://www.drewry.co.uk'

    

    # 初始化结果列�?
    all_news = []

    

    # 计算日期

    today_str = get_today_str()

    filter_date = target_date if target_date else today_str



    try:

        driver.get(base_url)

        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        time.sleep(3)



        # 获取页面源码

        web_content = driver.page_source

        soup = BeautifulSoup(web_content, 'html.parser')

        

        # 查找新闻容器

        news_container = soup.find('div', {'id': 'dwOpinion'})

        if not news_container:

            return []

        

        # 获取所有新闻标题和链接

        title_elems = news_container.find_all('h2', class_='aos-ArticleTitle')

        # 获取所有日期元�?
        date_elems = news_container.find_all('span', class_='aos-DS48-Teaser')

        

        # 遍历新闻

        for i, title_elem in enumerate(title_elems):

            # 提取链接和标�?
            link_elem = title_elem.find('a', href=True)

            if not link_elem:

                continue

            

            title = link_elem.get_text(strip=True)

            href = link_elem.get('href', '')

            

            # 处理相对链接

            if href and not href.startswith(('http://', 'https://')):

                link = urljoin(base_url, href)

            else:

                link = href

            

            # 提取日期（从对应�?span 元素�?
            date_text = ''

            if i < len(date_elems):

                date_text = date_elems[i].get_text(strip=True)

            

            formatted_date = parse_drewry_date(date_text)

            

            # 构建�?imarine 格式相同的新闻字�?
            news_item = {

                'title': title,

                'link': link,

                'date': formatted_date,

                'category': 'Drewry Research'  # 固定分类

            }

            

            # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）

            if filter_today:

                if not formatted_date or formatted_date != filter_date:

                    continue

            

            all_news.append(news_item)

            

            # 限制最多返回max_news条新�?
            if max_news and len(all_news) >= max_news:

                break

        

        print(f"[Drewry] 获取: {len(all_news)} ")

        return all_news

        

    except Exception as e:

        print(f"[Drewry] 爬取错误: {e}")

        import traceback

        traceback.print_exc()

        return []

    finally:

        driver.quit()





if __name__ == "__main__":

    driver_path = None  # 使用 webdriver-manager 自动下载驱动



    try:

        # 默认只返回今天的新闻

        news = asyncio.run(scrape_drewry_news(

            chrome_driver_path=driver_path,

            headless=True,

            filter_today=True,

            max_news=10

        ))

        

        print(f"\n共获取{len(news)} 条新闻")

        

        for i, item in enumerate(news, 1):

            print(f"{i}. [{item['date']}] {item['title']}")

            print(f"   {item['link']}")

        

    except Exception as e:

        print(f"程序执行失败: {e}")


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





async def scrape_cssc_news(chrome_driver_path, headless=True, filter_today=True, target_date=None, max_news=None):

    """

    爬取中船重工(CSSC)官网新闻数据



    Parameters:

    chrome_driver_path (str): chromedriver的完整路�?
    headless (bool): 是否使用无头模式，默认为True

    filter_today (bool): 是否筛选今日新闻，默认为True

    target_date (str): 目标日期，格式为'YYYY-MM-DD'，默认为None

    max_news (int): 最大返回新闻数量，默认�?0



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

    base_url = 'http://www.cssc.net.cn'

    news_page_url = 'http://www.cssc.net.cn/n5/index.html'

    

    # 初始化结果列
    all_news = []

    

    # 计算日期

    today_str = get_today_str()

    current_year = datetime.now().year

    filter_date = target_date if target_date else today_str



    try:

        driver.get(news_page_url)

        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        time.sleep(3)

        

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        

        # 只获取"船舶市场"板块下的新闻

        # 定位 class="mtjj" 的div（船舶市场板块的外层容器）

        ship_market_section = None

        for mtjj_div in soup.find_all('div', class_='mtjj'):

            if '船舶市场' in mtjj_div.get_text(strip=True):

                ship_market_section = mtjj_div

                break

        

        if not ship_market_section:

            print("[CSSC] 未找到'船舶市场'板块")

            return []

        

        # 在船舶市场板块内找到新闻列表容器

        news_container = ship_market_section.find('div', class_='mtjj_list')

        

        if not news_container:

            print("[CSSC] 未找到'船舶市场'新闻列表")

            return []

        

        # 查找所有新闻项

        news_items = news_container.find_all('li')

        

        for item in news_items:

            # 查找链接和标题
            link_elem = item.find('a', href=True)

            if not link_elem:

                continue

            

            title = link_elem.get('title', '').strip()

            href = link_elem.get('href', '')

            

            # 处理相对链接，构建完整URL

            link = urljoin(news_page_url, href) if href else ''

            

            # 查找日期

            date_elem = item.find('span')

            date_raw = date_elem.get_text(strip=True) if date_elem else ''

            

            # 将 MM-DD 转换为 YYYY-MM-DD

            date_str = date_raw

            if re.match(r'^\d{2}-\d{2}$', date_raw):

                date_str = f"{current_year}-{date_raw}"

            

            # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）

            if filter_today and date_str != filter_date:

                continue

            

            # 构建imarine 格式相同的新闻字段
            news_item = {

                'title': title,

                'link': link,

                'date': date_str,

                'category': '中船新闻'

            }

            all_news.append(news_item)

            

            # 限制最多返回max_news条新闻
            if max_news and len(all_news) >= max_news:

                break

        

        print(f"[CSSC] 获取: {len(all_news)}")

        return all_news



    except Exception as e:

        print(f"[CSSC] 爬取错误: {e}")

        import traceback

        traceback.print_exc()

        return []

    finally:

        driver.quit()





if __name__ == "__main__":

    driver_path = None  # 使用 webdriver-manager 自动下载驱动



    try:

        news = asyncio.run(scrape_cssc_news(

            chrome_driver_path=driver_path,

            headless=True,

            filter_today=True,

            max_news=10

        ))

        

        print(f"\n共获取{len(news)} 条新闻")

        for i, item in enumerate(news, 1):

            print(f"{i}. [{item['date']}] {item['title']}")

        

    except Exception as e:

        print(f"程序执行失败: {e}")


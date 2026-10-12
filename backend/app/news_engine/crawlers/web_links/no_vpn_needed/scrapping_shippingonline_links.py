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

from date_utils import get_today_str, get_yesterday_str, parse_date, is_today_or_recent

from datetime import datetime

import asyncio

import time

import re





def parse_shippingonline_date(date_text):

    """

    将日期文本从格式 "DD MMM YYYY" 转换�?"YYYY-MM-DD"

    例如: "17 Mar 2026" -> "2026-03-17"

    """

    if not date_text:

        return None

    

    date_text = date_text.strip()

    

    # 月份映射

    months = {

        'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,

        'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12

    }

    

    try:

        # 匹配格式: "17 Mar 2026"

        match = re.match(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})', date_text)

        if match:

            day, month_str, year = match.groups()

            month = months.get(month_str.capitalize(), 1)

            return f"{year}-{month:02d}-{int(day):02d}"

    except Exception as e:

        print(f"[Shippingonline] 日期解析错误: {e}")

    

    return None





async def scrape_shippingonline_news(chrome_driver_path, headless=True, filter_today=True, target_date=None, max_news=None):

    """

    爬取航运在线(Shipping Online)网站新闻数据



    Parameters:

    chrome_driver_path (str): chromedriver的完整路�?
    headless (bool): 是否使用无头模式，默认为True

    filter_today (bool): 是否筛选今日新闻，默认为True

    target_date (str): 目标日期，格式为'YYYY-MM-DD'，默认为None

    max_news (int): 最大新闻数量，默认�?0



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

    base_url = 'http://www.shippingonline.cn'

    news_page_url = f'{base_url}/news/index.asp'

    

    # 初始化结果列�?
    all_news = []

    

    # 计算日期

    today_str = get_today_str()

    filter_date = target_date if target_date else today_str



    try:

        driver.get(news_page_url)

        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        time.sleep(2)



        web_content = driver.page_source

        soup = BeautifulSoup(web_content, 'html.parser')

        

        # 查找表格中的所有行

        news_table = soup.find('table')

        if not news_table:

            print("[Shippingonline] 未找到新闻表格")

            return []

        

        news_rows = news_table.find_all('tr')

        

        for row in news_rows:

            # 查找所�?td 单元�?
            cells = row.find_all('td')

            if len(cells) < 2:

                # 跳过表头�?
                continue

            

            # 第一个单元格包含标题和链�?
            title_cell = cells[0]

            link_elem = title_cell.find('a', href=True)

            

            if not link_elem:

                continue

            

            title = link_elem.get_text(strip=True)

            href = link_elem.get('href', '')

            

            # 构建完整链接

            if href:

                link = urljoin(news_page_url, href)

            else:

                link = ''

            

            # 第二个单元格包含日期

            date_cell = cells[1]

            date_text = date_cell.get_text(strip=True)

            formatted_date = parse_shippingonline_date(date_text)

            

            # 构建�?imarine 格式相同的新闻字�?
            news_item = {

                'title': title,

                'link': link,

                'date': formatted_date,

                'category': '航运资讯'

            }

            

            # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）

            if filter_today:

                if not formatted_date or formatted_date != filter_date:

                    continue

            

            all_news.append(news_item)

            

            # 限制最多返回max_news条新�?
            if max_news and len(all_news) >= max_news:

                break

        

        return all_news



    except Exception as e:

        print(f"[Shippingonline] 爬取错误: {e}")

        import traceback

        traceback.print_exc()

        return []

    finally:

        driver.quit()





if __name__ == "__main__":

    driver_path = None  # 使用 webdriver-manager 自动下载驱动



    try:

        # 默认只返回今天的新闻

        news = asyncio.run(scrape_shippingonline_news(

            chrome_driver_path=driver_path,

            headless=True,

            filter_today=True,

            max_news=10

        ))

        

        print(f"[Shippingonline] 获取: {len(news)}条")

        

    except Exception as e:

        print(f"[Shippingonline] 程序执行失败: {e}")


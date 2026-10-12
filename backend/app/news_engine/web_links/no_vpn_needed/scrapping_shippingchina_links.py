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





def extract_date_from_text(text):

    """

    从文本中提取日期，格式：发布时间：YYYY-MM-DD

    支持全角和半角冒�?
    """

    if not text:

        return None

    # 匹配 "发布时间" 后跟全角或半角冒号和日期

    match = re.search(r'发布时间[�?](\d{4}-\d{2}-\d{2})', text)

    if match:

        return match.group(1)

    # 备用：直接匹配日期格�?
    match = re.search(r'(\d{4}-\d{2}-\d{2})', text)

    if match:

        return match.group(1)

    return None





def extract_title_from_text(text):

    """

    从文本中提取标题（发布时间之前的部分)
    """

    if not text:

        return ''

    # 移除"查看详情"后缀

    text = re.sub(r'查看详情', '', text)

    # 移除发布时间及之后的内容

    text = re.sub(r'发布时间', '', text)

    return text.strip()





async def scrape_shippingchina_news(chrome_driver_path=None, headless=True, filter_today=True, target_date=None, max_news=None):

    """

    爬取中国航运网新闻数�?


    Parameters:

    chrome_driver_path (str): chromedriver的完整路径，为None时使用系统默�?
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

    if chrome_driver_path:

        service = Service(executable_path=chrome_driver_path or os.getenv("CHROMEDRIVER_PATH", "/usr/lib64/chromium-browser/chromedriver"))

        driver = webdriver.Chrome(service=service, options=chrome_options)

    else:

        driver = webdriver.Chrome(options=chrome_options)

    driver.implicitly_wait(10)



    # 定义基础URL

    base_url = 'https://www.shippingchina.com'

    news_page_url = f'{base_url}/medium/news/index.html'

    

    # 初始化结果列�?
    all_news = []

    

    # 计算日期

    today_str = get_today_str()

    filter_date = target_date if target_date else today_str



    try:

        driver.get(news_page_url)

        

        # 等待页面加载

        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        time.sleep(3)

        

        # 获取页面源码

        web_content = driver.page_source

        soup = BeautifulSoup(web_content, 'html.parser')

        

        # 查找新闻列表

        news_list = soup.find('ul', class_='news-list')

        if not news_list:

            print("[Shippingchina] 未找到新闻列表")

            return []

        

        # 查找所有新闻项

        news_items = news_list.find_all('li')

        

        for item in news_items:

            # 查找链接

            link_elem = item.find('a', href=True)

            if not link_elem:

                continue

            

            href = link_elem.get('href', '')

            full_text = link_elem.get_text(strip=True)

            

            # 构建完整链接

            if href and not href.startswith(('http://', 'https://')):

                link = urljoin(base_url, href)

            else:

                link = href

            

            # 提取标题和日�?
            title = extract_title_from_text(full_text)

            date_str = extract_date_from_text(full_text)

            

            # 构建�?imarine 格式相同的新闻字�?
            news_item = {

                'title': title,

                'link': link,

                'date': date_str,

                'category': 'Shippingchina'

            }

            

            # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）

            if filter_today:

                if not date_str or date_str != filter_date:

                    continue

            

            all_news.append(news_item)

            

            # 限制最多返回max_news条新�?
            if max_news and len(all_news) >= max_news:

                break

        

        return all_news



    except Exception as e:

        print(f"[Shippingchina] 爬取错误: {e}")

        import traceback

        traceback.print_exc()

        return []

    finally:

        driver.quit()





if __name__ == "__main__":

    # 设为 None 使用系统默认�?ChromeDriver

    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    

    try:

        # 默认只返回今天的新闻

        news = asyncio.run(scrape_shippingchina_news(

            chrome_driver_path=driver_path,

            headless=True,

            filter_today=True,  # 只返回今天的新闻

            max_news=10

        ))

        

        print(f"[Shippingchina] 获取: {len(news)} 条")

        

    except Exception as e:

        print(f"[Shippingchina] 程序执行失败: {e}")


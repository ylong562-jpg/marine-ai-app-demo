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

import json

from date_utils import get_today_str, get_yesterday_str, parse_date, is_today_or_recent





def build_clarksons_url(article_id, base_url='https://www.clarksons.net/n/#/portal/insights/'):

    """

    根据 articleId 构建 Clarksons 新闻链接

    """

    return f"{base_url}{article_id}"





async def scrape_clarksons_news(chrome_driver_path, headless=True, filter_today=True, target_date=None, max_news=None):

    """

    爬取Clarksons Research网站新闻数据

    注意：Clarksons是付费订阅服务，部分内容可能需要登�?


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

    chrome_options.add_argument("--disable-web-security")

    chrome_options.add_argument("--window-size=1920,1080")

    chrome_options.add_argument(

        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")



    # 初始化浏览器驱动

    service = Service(executable_path=chrome_driver_path or os.getenv("CHROMEDRIVER_PATH", "/usr/lib64/chromium-browser/chromedriver"))

    driver = webdriver.Chrome(service=service, options=chrome_options)

    driver.implicitly_wait(10)



    # 定义URL

    base_url = 'https://www.clarksons.net'

    api_url = f'{base_url}/api/articles/insights'

    

    # 初始化结果列�?
    all_news = []

    

    # 计算日期

    today_str = get_today_str()

    filter_date = target_date if target_date else today_str

    

    try:

        driver.get(api_url)

        

        # 等待页面加载

        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        time.sleep(3)

        

        # 获取页面源码并解析JSON

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        

        # 查找pre标签中的JSON数据

        pre_tag = soup.find('pre')

        if pre_tag:

            json_text = pre_tag.get_text()

            articles = json.loads(json_text)

        else:

            # 如果没有pre标签，尝试直接解析body文本

            body_text = soup.find('body').get_text()

            articles = json.loads(body_text)

        

        # 解析每条新闻

        for article in articles:

            article_id = article.get('articleId')

            title = article.get('title', '').strip()

            date_published = article.get('datePublished', '')

            synopsis = article.get('synopsis', '')

            

            # 构建新闻链接

            news_url = build_clarksons_url(article_id)

            

            # 格式化日�?(YYYY-MM-DD)

            formatted_date = date_published if date_published else None

            

            # 构建�?imarine 格式相同的新闻字�?
            news_item = {

                'title': title,

                'link': news_url,

                'date': formatted_date,

                'category': 'Clarksons Insights'  # 固定分类

            }

            

            # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）

            if filter_today:

                if not formatted_date:

                    continue

                if formatted_date != filter_date:

                    continue

            

            all_news.append(news_item)

            

            # 限制最多返回max_news条新�?
            if max_news and len(all_news) >= max_news:

                break

            print(f"[Clarksons] 获取: {len(all_news)}")
        return all_news

        

    except Exception as e:

        print(f"[Clarksons] 爬取错误: {e}")

        import traceback

        traceback.print_exc()

        return []

    finally:

        driver.quit()





if __name__ == "__main__":

    driver_path = None  # 使用 webdriver-manager 自动下载驱动



    try:

        # 默认只返回今天的新闻

        news = asyncio.run(scrape_clarksons_news(

            chrome_driver_path=driver_path,

            headless=True,

            filter_today=True,

            max_news=10

        ))

        

        print(f"\n共获得{len(news)} 条新闻\n")

        

        for i, item in enumerate(news, 1):

            print(f"{i}. [{item['date']}] {item['title']}")

            print(f"   {item['link']}")

        

    except Exception as e:

        print(f"程序执行失败: {e}")


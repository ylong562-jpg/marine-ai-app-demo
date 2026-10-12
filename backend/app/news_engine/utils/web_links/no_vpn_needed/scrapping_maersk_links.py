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

from date_utils import get_today_str, get_yesterday_str, parse_date, is_today_or_recent, resolve_date_range, compute_scroll_times





def parse_maersk_date(date_text):

    """

    将日期文本从格式 "DD MMM YYYY" 转换�?"YYYY-MM-DD"

    例如: "18 Mar 2026" -> "2026-03-18"

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

        # 匹配格式: "18 Mar 2026" �?"9 Feb 2026"

        match = re.match(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})', date_text)

        if match:

            day, month_str, year = match.groups()

            month = months.get(month_str.capitalize(), 1)

            return f"{year}-{month:02d}-{int(day):02d}"

    except Exception as e:

        print(f"[MAERSK] 日期解析错误: {e}")

    

    return None





async def scrape_maersk_news(chrome_driver_path, headless=True, filter_today=False, target_date=None, max_news=None, start_date=None, end_date=None):

    """

    爬取马士�?Maersk)官网新闻数据



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

    base_url = 'https://www.maersk.com.cn'

    news_url = f'{base_url}/news'

    

    # 初始化结果列�?
    all_news = []

    

    # 计算日期

    today_str = get_today_str()

    filter_date = target_date if target_date else today_str

    # 统一解析日期范围（兼容旧版 filter_today=True）

    if filter_today and not target_date and not start_date and not end_date:

        target_date = today_str

    effective_start, effective_end, day_span = resolve_date_range(start_date, end_date, target_date)

    effective_scroll_times = compute_scroll_times(3, day_span, multiplier=2, max_scroll=30)

    print(f"[MAERSK] 目标日期范围: {effective_start} ~ {effective_end} (跨度 {day_span} 天), 滚动次数: {effective_scroll_times}")



    try:

        driver.get(news_url)

        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))



        # 滚动页面加载更多内容

        for i in range(effective_scroll_times):

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            time.sleep(2)



        # 获取页面源码

        web_content = driver.page_source

        soup = BeautifulSoup(web_content, 'html.parser')

        

        # 查找所有新闻项

        news_items = soup.find_all('div', {'class': 'p-section__news__teaser__content'})

        

        for item in news_items:

            # 查找分类

            category_elem = item.find('a', {'class': 'p-section__news__teaser__category'})

            category = category_elem.get_text(strip=True) if category_elem else 'News'

            

            # 查找标题和链�?
            title_elem = item.find('a', {'class': 'p-section__news__teaser__title'})

            if not title_elem:

                continue

            

            # 获取标题（从span或title属性）

            title_span = title_elem.find('span', {'class': 'p-section__news__teaser__title__line'})

            if title_span:

                title = title_span.get('title', '') or title_span.get_text(strip=True)

            else:

                title = title_elem.get_text(strip=True)

            

            # 获取链接

            href = title_elem.get('href', '')

            if href:

                link = urljoin(base_url, href)

            else:

                link = ''

            

            # 查找日期

            date_elem = item.find('div', {'class': 'p-section__news__teaser__timestamp'})

            date_text = date_elem.get_text(strip=True) if date_elem else ''

            formatted_date = parse_maersk_date(date_text)

            

            # 构建�?imarine 格式相同的新闻字�?
            news_item = {

                'title': title,

                'link': link,

                'date': formatted_date,

                'category': category

            }

            

            # 日期筛选逻辑 - 按日期范围过滤

            if effective_start and effective_end:

                if not formatted_date:

                    continue

                if formatted_date < effective_start or formatted_date > effective_end:

                    continue

            

            all_news.append(news_item)

            

            # 限制最多返回max_news条新�?
            if max_news and len(all_news) >= max_news:

                break

        

        return all_news



    except Exception as e:

        print(f"[MAERSK] 爬取错误: {e}")

        import traceback

        traceback.print_exc()

        return []

    finally:

        driver.quit()





if __name__ == "__main__":

    driver_path = None  # 使用 webdriver-manager 自动下载驱动



    try:

        # 示例：抓取最近 3 天

        news = asyncio.run(scrape_maersk_news(

            chrome_driver_path=driver_path,

            headless=True,

            start_date=(datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d'),

            end_date=datetime.now().strftime('%Y-%m-%d'),

            max_news=10

        ))

        

        print(f"[MAERSK] 获取: {len(news)} 条")

        

    except Exception as e:

        print(f"[MAERSK] 程序执行失败: {e}")


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


async def scrape_marinecircle_news(chrome_driver_path=None, headless=True, filter_today=True, target_date=None, max_news=None):
    """
    爬取海运圈聚焦(MarineCircle)官网船舶新闻数据

    Parameters:
    -----------
    chrome_driver_path : str
        ChromeDriver 的路径
    headless : bool
        是否使用无头模式，默认为 True
    filter_today : bool
        是否只返回今天的新闻，默认为 True
    target_date : str
        指定目标日期 (格式: YYYY-MM-DD)，如果提供则返回该日期的新闻
    max_news : int
        最大新闻数量，默认为 10

    Returns:
    --------
    list
        包含所有新闻字典的大列表，每个字典包含 'title', 'link', 'date', 'category' 四个键
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

    base_url = 'https://www.hyqfocus.com/jsp/news_classify.jsp?classify=3'

    today_str = get_today_str()
    filter_date = target_date if target_date else today_str

    all_news = []

    try:
        driver.get(base_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # 查找新闻列表容器
        news_container = soup.find('div', {'class': 'news_classify_bigbox'})
        if not news_container:
            print("[MARINECIRCLE] 未找到新闻列表容器")
            return []

        # 查找所有新闻项
        news_items = news_container.find_all('div', {'class': 'news-classify'})

        for item in news_items:
            # 查找标题
            title_elem = item.find('div', {'class': 'classify-title'})
            if not title_elem:
                continue

            title_link = title_elem.find('a')
            if not title_link:
                continue

            # 获取标题文本
            title_h1 = title_link.find('h1', {'class': 'title_hidden2'})
            if title_h1:
                title = title_h1.get_text(strip=True)
            else:
                title = title_link.get_text(strip=True)

            if not title:
                continue

            # 获取链接并拼接为完整URL
            href = title_link.get('href', '')
            if href:
                link = urljoin(base_url, href)
            else:
                continue

            # 查找日期
            date_elem = item.find('div', {'class': 'classify-time'})
            date_text = date_elem.find('span').get_text(strip=True) if date_elem and date_elem.find('span') else ''
            formatted_date = parse_date(date_text)

            # 构建与 maersk 格式相同的新闻字典
            news_item = {
                'title': title,
                'link': link,
                'date': formatted_date,
                'category': '船舶新闻'
            }

            # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）
            if filter_today:
                if not formatted_date or formatted_date != filter_date:
                    continue

            all_news.append(news_item)

            # 限制最多返回 max_news 条新闻
            if max_news and len(all_news) >= max_news:
                break

        return all_news

    except Exception as e:
        print(f"[MARINECIRCLE] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    try:
        # 默认只返回今天的新闻
        news = asyncio.run(scrape_marinecircle_news(
            chrome_driver_path=driver_path,
            headless=True,
            filter_today=True,  # 只返回今天的新闻
            max_news=10
        ))

        print(f"[MARINECIRCLE] 获取: {len(news)} 条")

        for i, item in enumerate(news, 1):
            print(f"\n{i}. [{item['date']}] {item['title']}")
            print(f"   {item['link']}")

    except Exception as e:
        print(f"[MARINECIRCLE] 程序执行失败: {e}")

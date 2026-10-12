import os
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

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_WDM = True
except ImportError:
    USE_WDM = False


def parse_date_text(date_text):
    """
    将 gCaptain 的日期文本转换为 YYYY-MM-DD 格式
    例如: 'Today' -> '2026-04-23'
          'Wednesday, April 22, 2026' -> '2026-04-22'
          'April 20, 2026' -> '2026-04-20'
    """
    if not date_text:
        return None

    date_text = date_text.strip()

    # 处理 "Today"
    if date_text.lower() == 'today':
        return datetime.now().strftime('%Y-%m-%d')

    # 去掉星期几前缀，例如 "Wednesday, April 22, 2026"
    if ',' in date_text:
        date_text = date_text.split(',', 1)[1].strip()

    # 匹配 "April 22, 2026" 格式
    try:
        dt = datetime.strptime(date_text, '%B %d, %Y')
        return dt.strftime('%Y-%m-%d')
    except ValueError:
        pass

    return None


async def scrape_gcaptain_news(chrome_driver_path=None, headless=True, filter_today=True, target_date=None):
    """
    爬取 gCaptain.com 新闻数据

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

    base_url = 'https://gcaptain.com/'
    all_news = []

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    # 如果指定了目标日期，使用目标日期；否则使用今天
    filter_date = target_date if target_date else today_str

    try:
        driver.get(base_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(5)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # 查找新闻列表容器
        content_loop = soup.find('div', {'class': 'content-loop'})
        if not content_loop:
            print("[gCaptain] 未找到新闻列表容器")
            return []

        # 遍历 content-loop 的子元素，逐个处理日期分隔和文章
        current_date = None
        for child in content_loop.children:
            if not hasattr(child, 'name'):
                continue

            # 日期分隔条
            if child.name == 'div' and 'hrdivider' in ' '.join(child.get('class', [])):
                date_span = child.find('span')
                if date_span:
                    date_text = date_span.get_text(strip=True)
                    if date_text:
                        parsed = parse_date_text(date_text)
                        if parsed:
                            current_date = parsed
                continue

            # 文章
            if child.name == 'div' and 'article' in ' '.join(child.get('class', [])):
                # 提取标题和链接
                headline_a = child.find('a', {'class': 'headline'}, href=True)
                if not headline_a:
                    continue

                h3 = headline_a.find('h3')
                if not h3:
                    continue

                title = h3.get_text(strip=True)
                link = headline_a['href']

                # 处理相对链接
                if link and not link.startswith('http'):
                    link = urljoin(base_url, link)

                # 提取分类
                category = ''
                cat_a = child.find('a', {'class': 'category'})
                if cat_a:
                    cat_span = cat_a.find('span', {'class': 'category'})
                    if cat_span:
                        category = cat_span.get_text(strip=True)
                    else:
                        category = cat_a.get_text(strip=True)

                # 如果 current_date 仍为空，尝试从文章内部 meta date 解析
                article_date = current_date
                if not article_date:
                    meta_date = child.find('div', {'class': 'meta'})
                    if meta_date:
                        date_span = meta_date.find('span', {'class': 'date'})
                        if date_span:
                            article_date = parse_date_text(date_span.get_text(strip=True))

                # 构建新闻字典
                news_item = {
                    'title': title,
                    'link': link,
                    'date': article_date,
                    'category': category if category else 'News'
                }

                # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）
                if filter_today:
                    if not article_date:
                        continue
                    if article_date != filter_date:
                        continue

                all_news.append(news_item)

        print(f"[gCaptain] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[gCaptain] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    # 默认只返回今天的新闻
    news = asyncio.run(scrape_gcaptain_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    today_str = datetime.now().strftime('%Y-%m-%d')

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] [{item['category']}] {item['title']}")
        print(f"   {item['link']}")

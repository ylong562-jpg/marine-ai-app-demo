import os
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

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_WDM = True
except ImportError:
    USE_WDM = False


def parse_date_text(day_text, month_text):
    """
    将 PartYard 的日期文本转换为 YYYY-MM-DD 格式
    例如: day='22', month='Jun' -> '2026-06-22'
    """
    if not day_text or not month_text:
        return None

    day_text = day_text.strip()
    month_text = month_text.strip()

    month_map = {
        'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
        'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
        'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'
    }

    month_num = month_map.get(month_text.lower())
    if not month_num:
        return None

    # 日期补零
    day = day_text.zfill(2)
    year = datetime.now().year

    return f"{year}-{month_num}-{day}"


async def scrape_partyard_news(chrome_driver_path=None, headless=True, filter_today=True, target_date=None):
    """
    爬取 PartYard 博客新闻数据

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

    base_url = 'https://www.partyard.eu'
    news_page_url = f'{base_url}/blog'
    all_news = []

    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    # 如果指定了目标日期，使用目标日期；否则使用今天
    filter_date = target_date if target_date else today_str

    try:
        driver.get(news_page_url)

        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # 查找所有文章
        articles = soup.find_all('article', class_=re.compile(r'post'))
        if not articles:
            print("[PartYard] 未找到新闻列表")
            return []

        for article in articles:
            # 提取日期
            date_div = article.find('div', class_='date')
            if date_div:
                day_elem = date_div.find('div', class_='day')
                month_elem = date_div.find('div', class_='month')
                day_text = day_elem.get_text(strip=True) if day_elem else ''
                month_text = month_elem.get_text(strip=True) if month_elem else ''
                formatted_date = parse_date_text(day_text, month_text)
            else:
                formatted_date = None

            # 提取标题和链接
            content_div = article.find('div', class_='content')
            if not content_div:
                continue

            link_elem = content_div.find('h5').find('a', href=True) if content_div.find('h5') else None
            if not link_elem:
                continue

            title = link_elem.get_text(strip=True)
            href = link_elem.get('href', '')

            # 处理相对链接，转换为完整链接
            if href:
                link = urljoin(news_page_url, href)
            else:
                link = ''

            # 提取分类
            category = ''
            cat_div = content_div.find('div', class_='categories')
            if cat_div:
                cat_a = cat_div.find('a')
                if cat_a:
                    category = cat_a.get_text(strip=True)
            if not category:
                category = 'News'

            # 构建新闻字典
            news_item = {
                'title': title,
                'link': link,
                'date': formatted_date,
                'category': category
            }

            # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）
            if filter_today:
                if not formatted_date:
                    continue
                if formatted_date != filter_date:
                    continue

            all_news.append(news_item)

        print(f"[PartYard] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[PartYard] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    output_path = os.path.join(base_dir, 'output', '临时.txt')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 默认只返回今天的新闻
    news = asyncio.run(scrape_partyard_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    today_str = datetime.now().strftime('%Y-%m-%d')

    lines = []
    lines.append(f"共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        lines.append(f"\n{i}. [{item['date']}] [{item['category']}] {item['title']}")
        lines.append(f"   {item['link']}")

    output_text = '\n'.join(lines)

    # 输出到控制台
    print(output_text)

    # 保存结果到指定文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(output_text)

    print(f"\n结果已保存到: {output_path}")

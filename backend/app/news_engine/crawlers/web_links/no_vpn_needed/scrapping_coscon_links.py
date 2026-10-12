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
import json

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_WDM = True
except ImportError:
    USE_WDM = False


def parse_status_dt(status_dt):
    """
    将 statusDt 转换为只保留年月日的格式
    例如: '2026-03-10 00:00:00' -> '2026-03-10'
    """
    if status_dt:
        try:
            dt = datetime.strptime(status_dt, '%Y-%m-%d %H:%M:%S')
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            return status_dt[:10] if len(status_dt) >= 10 else status_dt
    return None


def build_news_url(news_uuid, news_type_uuid, base_url='https://lines.coscoshipping.com'):
    """
    根据 newsUuid 和 newsTypeUuid 构建新闻链接
    """
    return f"{base_url}/home/News/detail/{news_uuid}/{news_type_uuid}?id={news_type_uuid}"


async def scrape_coscon_news(chrome_driver_path=None, headless=True, filter_today=False, target_date=None, start_date=None, end_date=None):
    """
    爬取中远海运集团(COSCO Shipping)官网新闻数据，支持按日期范围筛选

    Parameters:
        chrome_driver_path (str): ChromeDriver 路径
        headless (bool): 是否使用无头模式
        filter_today (bool): 是否仅筛选今天的新闻，已废弃
        target_date (str): 目标日期 YYYY-MM-DD
        start_date (str): 开始日期 YYYY-MM-DD
        end_date (str): 结束日期 YYYY-MM-DD
    """
    chrome_options = Options()
    chrome_options.binary_location = os.getenv("CHROME_BIN", "/usr/lib64/chromium-browser/chromium-browser")
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    service = Service(executable_path=chrome_driver_path or os.getenv("CHROMEDRIVER_PATH", "/usr/lib64/chromium-browser/chromedriver"))
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(10)

    base_url = 'https://lines.coscoshipping.com/homeapiak/cosNewss/findTypeNews/50000000000000232?currentPage=1&size=5&language=0'
    all_news = []
    today_str = datetime.now().strftime('%Y-%m-%d')

    # 统一解析日期范围（兼容旧版 filter_today=True）
    if filter_today and not target_date and not start_date and not end_date:
        target_date = today_str
    effective_start, effective_end, _ = resolve_date_range(start_date, end_date, target_date)
    print(f"[COSCON] 目标日期范围: {effective_start} ~ {effective_end}")

    try:
        driver.get(base_url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(5)
        
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        news_items = soup.find('pre').get_text().strip()
        data_dict = json.loads(news_items)
        
        if data_dict.get('code') == 200 and 'data' in data_dict:
            news_list = data_dict['data'].get('content', {}).get('content', [])
            total_count = len(news_list)
            
            for item in news_list:
                news_uuid = item.get('newsUuid')
                news_type_uuid = item.get('newsTypeUuid')
                title = item.get('title')
                status_dt = item.get('statusDt')
                
                formatted_date = parse_status_dt(status_dt)
                
                if effective_start and effective_end:
                    if not formatted_date:
                        continue
                    if formatted_date < effective_start or formatted_date > effective_end:
                        continue
                
                news_url = build_news_url(news_uuid, news_type_uuid)
                all_news.append({
                    'title': title,
                    'link': news_url,
                    'date': formatted_date
                })
            
            print(f"[COSCON] 获取: {len(all_news)} 条")
        
        return all_news

    except Exception as e:
        print(f"[COSCON] 爬取错误: {e}")
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动
    
    news = asyncio.run(scrape_coscon_news(
        chrome_driver_path=driver_path,
        headless=True,
        start_date=(datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d'),
        end_date=datetime.now().strftime('%Y-%m-%d')
    ))
    
    print(f"\n共 {len(news)} 条新闻")
    for item in news:
        print(f"  - {item['date']} | {item['title']}")

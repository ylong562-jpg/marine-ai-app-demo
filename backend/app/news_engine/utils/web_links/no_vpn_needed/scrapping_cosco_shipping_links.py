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


def parse_date_text(date_text, year=None):
    """
    将日期文本转换为 YYYY-MM-DD 格式
    例如: '03-20' -> '2026-03-20'
    """
    if not date_text:
        return None
    
    # 如果没有提供年份，使用当前年份
    if year is None:
        year = datetime.now().year
    
    # 清理日期文本
    date_text = date_text.strip()
    
    # 匹配 MM-DD 格式
    try:
        if re.match(r'^\d{2}-\d{2}$', date_text):
            month, day = date_text.split('-')
            return f"{year}-{month}-{day}"
        elif re.match(r'^\d{4}-\d{2}-\d{2}$', date_text):
            return date_text
    except:
        pass
    
    return None


async def scrape_cosco_shipping_news(chrome_driver_path=None, headless=True, filter_today=True, target_date=None):
    """
    爬取中远海运集团(COSCO Shipping)官网新闻数据
    
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
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


    service = Service(executable_path=chrome_driver_path or os.getenv("CHROMEDRIVER_PATH", "/usr/lib64/chromium-browser/chromedriver"))
    
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(10)

    base_url = 'https://www.coscoshipping.com'
    news_page_url = f'{base_url}/col/col6855/index.html'
    all_news = []
    
    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    current_year = today.year
    
    # 如果指定了目标日期，使用目标日期；否则使用今天
    filter_date = target_date if target_date else today_str

    try:
        driver.get(news_page_url)
        
        # 等待页面加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)
        
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        
        # 查找所有新闻列表
        news_lists = soup.find_all('ul', {'class': 'news-list'})
        
        for news_list in news_lists:
            news_items = news_list.find_all('li')
            
            for item in news_items:
                # 提取链接和标题
                link_elem = item.find('a', href=True)
                if not link_elem:
                    continue
                
                title = link_elem.get('title', '').strip() or link_elem.get_text(strip=True)
                href = link_elem.get('href', '')
                
                # 处理相对链接
                if href and not href.startswith(('http://', 'https://')):
                    link = urljoin(base_url, href)
                else:
                    link = href
                
                # 提取日期
                date_elem = item.find('span')
                date_text = date_elem.get_text(strip=True) if date_elem else ''
                formatted_date = parse_date_text(date_text, current_year)
                
                # 构建与 imarine 格式相同的新闻字典
                news_item = {
                    'title': title,
                    'link': link,
                    'date': formatted_date,
                    'category': '航运新闻'  # COSCO 没有分类信息，使用默认分类
                }
                
                # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）
                if filter_today:
                    if not formatted_date:
                        continue
                    if formatted_date != filter_date:
                        continue
                
                all_news.append(news_item)
        
        print(f"[COSCO Shipping] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[COSCO Shipping] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    # 设为 None 使用 webdriver-manager 自动下载驱动
    driver_path = None  # 使用 webdriver-manager 自动下载驱动
    
    # 默认只返回今天的新闻
    news = asyncio.run(scrape_cosco_shipping_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))
    
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    print(f"\n共获取 {len(news)} 条新闻")
    
    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

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


def parse_date_from_detail(soup):
    """
    从新闻详情页解析日期
    尝试多种常见的日期格式和位置
    """
    if not soup:
        return None
    text = soup.find('div',{'id':'div_introductionleft'}).get_text().strip()
    date = text.strip().split()[-1]
    
    return date


def parse_date_text(date_text):
    """
    将日期文本转换为 YYYY-MM-DD 格式
    """
    if not date_text:
        return None
    
    date_text = date_text.strip()
    
    # 尝试匹配 ISO 格式 (2026-03-27T10:30:00 或 2026-03-27)
    iso_match = re.match(r'(\d{4})-(\d{2})-(\d{2})', date_text)
    if iso_match:
        return f"{iso_match.group(1)}-{iso_match.group(2)}-{iso_match.group(3)}"
    
    # 尝试匹配中文格式 (2026年3月27日)
    cn_match = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date_text)
    if cn_match:
        return f"{cn_match.group(1)}-{int(cn_match.group(2)):02d}-{int(cn_match.group(3)):02d}"
    
    return None


async def scrape_csi_news(chrome_driver_path=None, headless=True, filter_today=True, target_date=None):
    """
    爬取航运信息网(CSI)新闻数据
    
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

    base_url = 'https://www.csi.com.cn'
    news_page_url = f'{base_url}/index.aspx'
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
        
        # 查找新闻容器
        news_container = soup.find('div', {'id': 'div_hyzhzx_content'})
        if not news_container:
            return []

        # 获取所有新闻项
        news_items = news_container.find_all('li')
        
        # 只取前3条
        news_items = news_items[:3]

        for idx, item in enumerate(news_items, 1):
            try:
                # 提取链接和标题
                link_elem = item.find('a', href=True)
                if not link_elem:
                    continue

                title = link_elem.get_text(strip=True)
                href = link_elem.get('href', '')
                
                # 处理相对链接
                if href:
                    link = urljoin(news_page_url, href)
                else:
                    continue

                # 二次爬取：访问详情页获取日期
                formatted_date = None
                try:
                    driver.get(link)
                    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    time.sleep(2)
                    
                    detail_soup = BeautifulSoup(driver.page_source, 'html.parser')
                    formatted_date = parse_date_from_detail(detail_soup)
                        
                except Exception as e:
                    formatted_date = None

                # 构建新闻字典
                news_item = {
                    'title': title,
                    'link': link,
                    'date': formatted_date
                }

                # 日期筛选逻辑 - 只保留指定日期的新闻（默认今天）
                if filter_today:
                    if not formatted_date:
                        continue
                    if formatted_date != filter_date:
                        continue

                all_news.append(news_item)
                
                # 返回列表页继续处理下一条
                if idx < len(news_items):
                    driver.get(news_page_url)
                    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    time.sleep(1)

            except Exception as e:
                continue

        print(f"[CSI] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[CSI] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    # 设为 None 使用 webdriver-manager 自动下载驱动
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    # 默认只返回今天的新闻
    news = asyncio.run(scrape_csi_news(
        chrome_driver_path=driver_path,
        headless=True,
        filter_today=True  # 只返回今天的新闻
    ))

    today_str = datetime.now().strftime('%Y-%m-%d')

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] {item['title']}")
        print(f"   {item['link']}")

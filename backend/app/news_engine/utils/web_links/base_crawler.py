"""
爬虫基础类 - 提供通用功能
"""

from bs4 import BeautifulSoup
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urljoin
import os
import time
import asyncio

from date_utils import get_today_str, get_yesterday_str, parse_date, extract_date_from_relative, is_today_or_recent


class BaseCrawler:
    """爬虫基础类"""
    
    def __init__(self, chrome_driver_path, headless=True):
        self.driver_path = chrome_driver_path
        self.headless = headless
        self.driver = None
        
    def init_driver(self):
        """初始化浏览器驱动"""
        chrome_options = Options()
        chrome_options.binary_location = os.getenv(
            'CHROME_BIN', '/usr/lib64/chromium-browser/chromium-browser'
        )
        if self.headless:
            chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-software-rasterizer")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        chromedriver_path = self.driver_path or os.getenv(
            'CHROMEDRIVER_PATH', '/usr/lib64/chromium-browser/chromedriver'
        )
        service = Service(executable_path=chromedriver_path)
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self.driver.implicitly_wait(10)
        return self.driver
    
    def close_driver(self):
        """关闭浏览器驱动"""
        if self.driver:
            self.driver.quit()
            self.driver = None
    
    def fetch_page(self, url, wait_time=3):
        """获取页面内容"""
        self.driver.get(url)
        WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(wait_time)
        return self.driver.page_source
    
    def fetch_detail_page_date(self, url, date_selectors=None):
        """
        进入详情页获取日期
        
        Parameters:
        url (str): 详情页URL
        date_selectors (list): 日期元素选择器列表
        
        Returns:
        str: 日期字符串 YYYY-MM-DD
        """
        try:
            self.driver.get(url)
            WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(1)
            
            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            
            # 默认选择器
            default_selectors = [
                'time[datetime]',
                '.date',
                '.publish-date',
                '.post-date',
                '[class*="date"]',
                '[class*="time"]',
            ]
            
            selectors = date_selectors or default_selectors
            
            for selector in selectors:
                elem = soup.select_one(selector)
                if elem:
                    # 尝试获取datetime属性
                    date_str = elem.get('datetime', '')
                    if date_str:
                        return parse_date(date_str[:10])
                    # 否则获取文本
                    date_str = elem.get_text(strip=True)
                    if date_str:
                        parsed = parse_date(date_str)
                        if parsed:
                            return parsed
            
            return None
        except Exception as e:
            print(f"    获取详情页日期失败: {str(e)[:50]}")
            return None
    
    def is_valid_date(self, date_str, max_days=2):
        """
        检查日期是否是今天或最近几天
        
        Parameters:
        date_str (str): 日期字符串
        max_days (int): 往前推几天
        
        Returns:
        bool: 是否有效
        """
        return is_today_or_recent(date_str, max_days)


async def crawl_with_limits(crawl_func, driver_path, max_news=None, headless=True, **kwargs):
    """
    带限制的爬虫包装函数
    
    Parameters:
    crawl_func: 爬虫函数
    driver_path: chromedriver路径
    max_news (int): 最大新闻数量
    headless (bool): 是否无头模式
    **kwargs: 其他参数
    
    Returns:
    list: 新闻列表（最多max_news条）
    """
    try:
        news_list = await crawl_func(driver_path, headless=headless, **kwargs)
        if max_news and news_list and len(news_list) > max_news:
            print(f"  截断到前 {max_news} 条新闻")
            return news_list[:max_news]
        return news_list
    except Exception as e:
        print(f"  爬取失败: {str(e)[:100]}")
        return []


if __name__ == "__main__":
    print("基础爬虫类已加载")

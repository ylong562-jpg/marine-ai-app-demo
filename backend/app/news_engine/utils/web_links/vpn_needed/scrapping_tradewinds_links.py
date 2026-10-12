import os
from bs4 import BeautifulSoup
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime, timedelta
import asyncio
import time
import re
from date_utils import get_today_str, resolve_date_range, compute_scroll_times


def parse_tradewinds_date(date_text):
    """
    将 TradeWinds 日期文本转换为 YYYY-MM-DD 格式
    例如: '30.03.2026' -> '2026-03-30'
          '2026-03-30T05:11:53+00:00' -> '2026-03-30'
    """
    if not date_text:
        return None
    
    date_text = date_text.strip()
    
    # 尝试匹配 ISO 格式 '2026-03-30T05:11:53+00:00'
    try:
        if 'T' in date_text:
            date_obj = datetime.fromisoformat(date_text.replace('Z', '+00:00'))
            return date_obj.strftime('%Y-%m-%d')
    except ValueError:
        pass
    
    # 尝试匹配 '30.03.2026' 格式 (DD.MM.YYYY)
    try:
        date_obj = datetime.strptime(date_text, '%d.%m.%Y')
        return date_obj.strftime('%Y-%m-%d')
    except ValueError:
        pass
    
    # 尝试匹配 '2026-03-30' 格式
    try:
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_text):
            return date_text
    except:
        pass
    
    return None


async def scrape_tradewinds_news(chrome_driver_path, headless=True, filter_today=False, target_date=None, max_news=None, start_date=None, end_date=None):
    """
    爬取TradeWinds网站的新闻数据，支持按日期范围筛选

    Parameters:
    -----------
    chrome_driver_path : str
        ChromeDriver 的路径
    headless : bool
        是否使用无头模式，默认为 True
    filter_today : bool
        是否仅筛选今天的新闻，已废弃，建议使用日期范围参数
    target_date : str
        指定目标日期 (格式: YYYY-MM-DD)
    max_news : int
        最多返回的新闻条数，默认为10
    start_date : str
        开始日期 (格式: YYYY-MM-DD)
    end_date : str
        结束日期 (格式: YYYY-MM-DD)

    Returns:
    --------
    list : 包含所有新闻字典的列表，每个字典包含 'title', 'link', 'date', 'category' 四个键
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
    base_url = 'https://www.tradewindsnews.com'
    news_page_url = f'{base_url}/latest'
    
    # 初始化结果列表
    all_news = []
    seen_links = set()  # 用于去重

    # 获取今天的日期
    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    
    # 如果指定了目标日期，使用目标日期；否则使用今天
    filter_date = target_date if target_date else today_str

    # 统一解析日期范围（兼容旧版 filter_today=True）
    if filter_today and not target_date and not start_date and not end_date:
        target_date = today_str
    effective_start, effective_end, day_span = resolve_date_range(start_date, end_date, target_date)
    effective_scroll_times = compute_scroll_times(3, day_span, multiplier=2, max_scroll=30)
    print(f"[TradeWinds] 目标日期范围: {effective_start} ~ {effective_end} (跨度 {day_span} 天), 滚动次数: {effective_scroll_times}")

    try:
        # 访问TradeWinds首页
        driver.get(news_page_url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        # 滚动页面加载更多内容
        for i in range(effective_scroll_times):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

        # 获取页面源码
        web_content = driver.page_source
        soup = BeautifulSoup(web_content, 'html.parser')
        
        # 查找新闻容器
        news_container = soup.find('div', {'class': 'dn-latest'})
        if not news_container:
            print("[TradeWinds] 未找到新闻容器")
            return []
        
        # 查找所有新闻卡片
        news_items = news_container.find_all('a', {'class': 'dn-teaser-card'})
        
        for item in news_items:
            # 提取链接
            href = item.get('href', '')
            if not href:
                continue
            
            # 处理相对链接，转换为完整链接
            if href.startswith('http'):
                link = href
            else:
                link = base_url + href if not href.startswith('/') else base_url + href
            
            # 去重：跳过已处理过的链接
            if link in seen_links:
                continue
            seen_links.add(link)
            
            # 提取标题
            title_elem = item.find('h3', {'class': 'dn-headline--subhead'})
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            
            # 提取日期 - 优先从 data-datetime 属性获取
            date_elem = item.find('span', {'class': 'dn-date-time'})
            date_text = ''
            if date_elem:
                # 优先使用 data-datetime 属性
                date_text = date_elem.get('data-datetime', '')
                if not date_text:
                    date_text = date_elem.get_text(strip=True)
            
            formatted_date = parse_tradewinds_date(date_text)
            
            # 提取分类（在 meta div 中的第一个 span）
            category = '国际航运新闻'  # 默认分类
            meta_div = item.find('div', {'class': 'meta'})
            if meta_div:
                spans = meta_div.find_all('span')
                for span in spans:
                    span_class = span.get('class', [])
                    # 跳过日期元素，找到分类标签
                    if 'dn-date-time' not in span_class:
                        category_text = span.get_text(strip=True)
                        if category_text and category_text != '':
                            # 将 Finance 分类转换为中文
                            if category_text == 'Finance':
                                category = '公司金融'
                            else:
                                category = category_text
                            break
            
            # 构建新闻字典
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
            
            # 限制最大新闻数量
            if max_news and len(all_news) >= max_news:
                break
        
        print(f"[TradeWinds] 获取: {len(all_news)} 条")
        return all_news

    except Exception as e:
        print(f"[TradeWinds] 爬取错误: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    # 示例：抓取最近 3 天
    news = asyncio.run(scrape_tradewinds_news(
        chrome_driver_path=driver_path,
        headless=True,
        start_date=(datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d'),
        end_date=datetime.now().strftime('%Y-%m-%d'),
        max_news=10
    ))

    today_str = datetime.now().strftime('%Y-%m-%d')

    print(f"\n共获取 {len(news)} 条新闻")

    for i, item in enumerate(news, 1):
        print(f"\n{i}. [{item['date']}] [{item['category']}] {item['title']}")
        print(f"   {item['link']}")

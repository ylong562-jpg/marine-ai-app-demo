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
from date_utils import get_today_str, get_yesterday_str, parse_date, resolve_date_range, compute_scroll_times


async def scrape_imarine_news(chrome_driver_path, scroll_times=5, pause_time=4, headless=True, filter_today=False, max_news=None, target_date=None, start_date=None, end_date=None):
    """
    爬取imarine.cn网站的新闻数据，支持按日期范围筛选

    Parameters:
    chrome_driver_path (str): chromedriver的完整路径
    scroll_times (int): 滚动次数，默认为5次
    pause_time (int): 每次滚动后的等待时间，默认为4秒
    headless (bool): 是否使用无头模式，默认为True
    filter_today (bool): 是否仅筛选今天的新闻，已废弃，建议使用日期范围参数
    max_news (int): 最大返回新闻数量
    target_date (str): 目标日期 YYYY-MM-DD（单日）
    start_date (str): 开始日期 YYYY-MM-DD
    end_date (str): 结束日期 YYYY-MM-DD

    Returns:
    list: 包含所有新闻字典的大列表

    Raises:
    Exception: 当爬取过程中出现错误时抛出异常
    """
    # 设置Chrome选项
    chrome_options = Options()
    chrome_options.binary_location = os.getenv("CHROME_BIN", "/usr/lib64/chromium-browser/chromium-browser")
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # 初始化浏览器驱动
    service = Service(executable_path=chrome_driver_path or os.getenv("CHROMEDRIVER_PATH", "/usr/lib64/chromium-browser/chromedriver"))
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(5)

    # 定义基础URL
    base_url = 'https://www.imarine.cn'
    # 修改：使用单一列表存储所有新闻
    all_news = []

    # 获取今日日期字符串（格式如：2025-12-30）
    today_str = get_today_str()
    yesterday = get_yesterday_str()

    # 统一解析日期范围（兼容旧版 filter_today=True）
    if filter_today and not target_date and not start_date and not end_date:
        target_date = today_str
    effective_start, effective_end, day_span = resolve_date_range(start_date, end_date, target_date)

    # 根据日期跨度动态增加滚动次数，获取更多新闻
    effective_scroll_times = compute_scroll_times(scroll_times, day_span, multiplier=2, max_scroll=30)
    print(f"[IMARINE] 目标日期范围: {effective_start} ~ {effective_end} (跨度 {day_span} 天), 滚动次数: {effective_scroll_times}")

    try:
        # 访问目标网站
        driver.get(f"{base_url}/news")

        # 等待页面基础结构加载
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        # 滚动函数
        def scroll_to_bottom(driver, scroll_times, pause_time):
            """固定滚动指定次数来加载动态内容"""
            for i in range(scroll_times):
                # 滚动到页面底部
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                # 等待新内容加载
                time.sleep(pause_time)

        # 执行滚动
        scroll_to_bottom(driver, effective_scroll_times, pause_time)

        # 获取页面源码并解析
        web_content = driver.page_source
        soup = BeautifulSoup(web_content, 'html.parser')

        # 提取新闻数据
        news_items = soup.find_all(class_='entry-title')

        for item in news_items:
            title_link = item.find('a')
            if title_link:
                title = title_link.get_text(strip=True)
                link = title_link.get('href', '')
                # 处理相对链接
                if link and not link.startswith(('http://', 'https://')):
                    link = urljoin(base_url, link)

                # 查找新闻容器并提取其他信息
                news_container = item.find_parent('div')
                if news_container:
                    category_elem = news_container.find(class_='entry-category')
                    category = category_elem.get_text(strip=True) if category_elem else "未知分类"

                    # 如果 category 是"船厂订单"，替换为 "新造船订单"
                    if category == "船厂订单":
                        category = "新造船订单"

                    date_elem = news_container.find(class_='entry-date')
                    if date_elem:
                        date_text = date_elem.get_text(strip=True)
                        date_str = parse_date(date_text)
                    else:
                        date_str = None

                    # 日期筛选逻辑 - 按日期范围过滤
                    if effective_start and effective_end:
                        if not date_str:
                            continue  # 跳过无法解析日期的新闻
                        if date_str < effective_start or date_str > effective_end:
                            continue  # 跳过范围外的新闻

                    # 创建新闻字典（保留分类信息作为字段）
                    news_dict = {
                        'title': title,
                        'link': link,
                        'date': date_str,
                        'category': category  # 将分类作为字典的一个字段保留
                    }

                    # 修改：直接添加到统一列表
                    all_news.append(news_dict)

                    # 限制最多返回max_news条新闻
                    if max_news and len(all_news) >= max_news:
                        return all_news  # 返回单一列表

        return all_news  # 返回单一列表

    except Exception as e:
        print(f"[IMARINE] 爬取错误: {e}")
        raise
    finally:
        # 确保关闭浏览器
        driver.quit()


if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    try:
        # 调用函数（示例：抓取最近 3 天）
        today_news = asyncio.run(scrape_imarine_news(
            chrome_driver_path=driver_path,
            scroll_times=5,
            pause_time=4,
            headless=True,
            start_date=(datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d'),
            end_date=datetime.now().strftime('%Y-%m-%d')
        ))

        # 输出结果
        print(f"[IMARINE] 获取: {len(today_news)}条")

    except Exception as e:
        print(f"[IMARINE] 程序执行失败: {e}")

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
import time
import random
import asyncio
import re
import sys
import os
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.ssl_ import create_urllib3_context
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# 添加项目根目录到路径，以便导入 title_utils
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from date_utils import get_today_str, parse_date, is_today_or_recent
from title_utils import normalize_title, get_title_signature


async def scrape_eworldship_news(chrome_driver_path, headless=True, filter_today=True,
                                  target_date=None, max_news=None, extract_dates=True):
    """
    爬取国际船舶网的新闻数据并提取日期与正文信息

    Parameters:
    chrome_driver_path (str): chromedriver的完整路径
    headless (bool): 是否使用无头模式，默认为True
    extract_dates (bool): 是否提取日期信息，默认为True
    target_date (str): 目标日期，格式为'YYYY-MM-DD'，默认为None（获取今天/昨天的新闻）
    max_news (int): 最大新闻数量，默认为None表示不限制

    Returns:
    list: 包含所有新闻字典的大列表，每个字典包含'title', 'link', 'date', 'category', 'content'等键
    """
    # 初始化浏览器驱动
    service = Service(executable_path=chrome_driver_path or os.getenv("CHROMEDRIVER_PATH", "/usr/lib64/chromium-browser/chromedriver"))

    chrome_options = Options()
    chrome_options.binary_location = os.getenv("CHROME_BIN", "/usr/lib64/chromium-browser/chromium-browser")
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    # 反检测：隐藏 Selenium 自动化特征
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(5)

    # 定义基础URL
    base_url = 'https://www.eworldship.com/'

    # 初始化结果列表（取消分类，使用单一列表）
    all_news = []
    # 用于去重的集合：已采集的 URL 和已采集的标准化标题签名
    seen_urls = set()
    seen_title_signatures = set()

    # 计算日期范围
    today = get_today_str()

    # 内部函数：尝试从列表项中提取日期
    def extract_date_from_list_item(item):
        """尝试从列表项(li)中直接提取日期，避免不必要的详情页访问"""
        # 优先查找可能包含日期的 span/em/i/time/div 标签
        for tag in item.find_all(['span', 'em', 'i', 'time', 'div']):
            text = tag.get_text(strip=True)
            match = re.search(r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})[日]?', text)
            if match:
                try:
                    year, month, day = match.groups()
                    date_obj = datetime(int(year), int(month), int(day))
                    return date_obj.strftime('%Y-%m-%d')
                except ValueError:
                    continue
        # 尝试从整个文本中提取
        full_text = item.get_text(strip=True)
        match = re.search(r'(\d{4})-(\d{2})-(\d{2})', full_text)
        if match:
            try:
                date_obj = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                return date_obj.strftime('%Y-%m-%d')
            except ValueError:
                pass
        return None

    class _WeakSSLAdapter(HTTPAdapter):
        """降低 SSL 安全等级以兼容 eworldship 旧证书（DH_KEY_TOO_SMALL）。"""
        def init_poolmanager(self, *args, **kwargs):
            ctx = create_urllib3_context(ciphers="DEFAULT:@SECLEVEL=1")
            ctx.set_ciphers("DEFAULT:!DH")
            ctx.check_hostname = False
            kwargs["ssl_context"] = ctx
            return super().init_poolmanager(*args, **kwargs)

    def _fetch_detail_html(news_url):
        """使用 requests + Selenium cookies 获取详情页 GBK 源码。"""
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        cookies = driver.get_cookies()
        session = requests.Session()
        session.mount("https://", _WeakSSLAdapter())
        for c in cookies:
            session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
            )
        headers = {
            "User-Agent": driver.execute_script("return navigator.userAgent") or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = session.get(news_url, headers=headers, timeout=30, verify=False)
        resp.raise_for_status()
        # eworldship 使用 GBK/GB2312 编码
        return resp.content.decode("gbk", errors="ignore")

    # 内部函数：从新闻详情页提取日期、标题与正文
    def extract_news_detail(news_url):
        """
        从新闻详情页提取日期、标题和正文内容。
        Returns: (date_str, title_str, content)
        """
        try:
            # 先让 Selenium 访问详情页，确保会话/跳转正常
            driver.get(news_url)
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            # 再用 requests + cookies 获取正确编码的源码（Selenium page_source 对 GBK 正文会乱码）
            web_content = _fetch_detail_html(news_url)
            soup = BeautifulSoup(web_content, 'html.parser')

            # 提取标题
            title_str = ""
            title_tag = soup.find('title')
            if title_tag:
                title_str = title_tag.get_text(strip=True).split(" - ")[0]

            # 提取日期信息 - 根据实际页面结构调整选择器
            date_str = "未知日期"
            date_element = soup.find('span', class_='ss01')
            if date_element:
                date_text = date_element.get_text().strip()
                # 只保留年月日信息
                try:
                    # 尝试解析日期格式 "2025-12-30 06:29:27"
                    date_obj = datetime.strptime(date_text, '%Y-%m-%d %H:%M:%S')
                    date_str = date_obj.strftime('%Y-%m-%d')  # 只返回年月日
                except ValueError:
                    # 如果格式不匹配，尝试其他可能的日期格式
                    try:
                        date_obj = datetime.strptime(date_text.split()[0], '%Y-%m-%d')
                        date_str = date_obj.strftime('%Y-%m-%d')
                    except:
                        date_str = date_text.split()[0]  # 返回第一个部分作为日期

            # 提取正文内容
            content = ""
            content_selectors = [
                '#nArticle .content',
                '#article_content',
                '.content-detail',
                '.article-content',
                '.news-content',
                '.content',
                '#content',
                'article',
                '.main-content',
                '.detail',
            ]
            content_elem = None
            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    break

            if not content_elem:
                content_elem = soup.body

            if content_elem:
                # 移除脚本、样式、导航、页脚等噪声
                for tag in content_elem.find_all(['script', 'style', 'nav', 'footer', 'aside']):
                    tag.decompose()
                for cls in ['share', 'social', 'related', 'comment', 'sidebar', 'ad', 'advertisement', 'header', 'menu']:
                    for tag in content_elem.find_all(class_=lambda c: c and cls in c.lower()):
                        tag.decompose()

                paragraphs = []
                for p in content_elem.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'li']):
                    text = p.get_text(strip=True)
                    if text and len(text) > 5:
                        paragraphs.append(text)
                content = '\n'.join(paragraphs)

            return date_str, title_str, content
        except Exception as e:
            print(f"[国际船舶网] 详情页提取失败 {news_url}: {str(e)[:80]}")
            return "未知日期", "", ""

    # 获取目标日期（在函数开头就确定，避免时序问题）
    target_date_str = target_date if target_date else (today if filter_today else None)

    # 内部函数：检查日期是否符合要求（保留今天或指定目标日期）
    def is_date_valid(news_date):
        """检查新闻日期是否为今天（或指定的目标日期）"""
        if not news_date or news_date == "未知日期":
            return False
        if not filter_today:
            return True
        if target_date_str:
            return news_date == target_date_str
        # 只保留今天的新闻
        return news_date == today

    try:
        # 访问目标网站
        driver.get(base_url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        # 获取页面源码并解析
        web_content = driver.page_source
        soup = BeautifulSoup(web_content, 'html.parser')

        # 1. 爬取订单动态
        order_dynamic_div = soup.find('div', class_='contentL300')
        if order_dynamic_div:
            news_ul = order_dynamic_div.find('ul')
            if news_ul:
                news_items = news_ul.find_all('li')
                for item in news_items:
                    a_tag = item.find('a')
                    if a_tag:
                        title = a_tag.get_text(strip=True)
                        link = a_tag.get('href', '').strip()
                        full_link = urljoin(base_url, link)

                        # 跳过无效链接
                        if not full_link or full_link == base_url:
                            continue

                        # 标准化标题
                        title_signature = get_title_signature(title)

                        # 去重：相同 URL 或相同标准化标题跳过
                        if full_link in seen_urls:
                            print(f"[国际船舶网] 跳过重复URL: {full_link}")
                            continue
                        if title_signature and title_signature in seen_title_signatures:
                            print(f"[国际船舶网] 跳过重复标题: {title[:40]}...")
                            continue

                        # 提取日期与正文
                        date = "未知日期"
                        content = ""
                        if extract_dates:
                            # 先尝试从列表项提取日期，减少不必要的详情页访问
                            list_date = extract_date_from_list_item(item)
                            if list_date:
                                date = list_date

                            # 无论列表是否有日期，都进入详情页获取准确日期和正文
                            time.sleep(random.uniform(1, 2))
                            detail_date, detail_title, content = extract_news_detail(full_link)
                            # 如果详情页日期获取失败，回退列表日期
                            if detail_date == "未知日期" and list_date:
                                detail_date = list_date
                            date = detail_date
                            # 详情页标题通常更准确（列表页在 GBK 解码异常时可能乱码）
                            if detail_title:
                                title = detail_title

                        news_dict = {
                            'title': title,
                            'link': full_link,
                            'date': date,
                            'category': '新造船订单',
                            'content': content,
                            'source_platform': '国际船舶网'
                        }

                        # 日期筛选逻辑：只保留今天/昨天的新闻
                        if is_date_valid(date):
                            seen_urls.add(full_link)
                            if title_signature:
                                seen_title_signatures.add(title_signature)
                            all_news.append(news_dict)
                            if max_news and len(all_news) >= max_news:
                                print(f"[国际船舶网] 已获取 {len(all_news)} 条新闻，达到上限")
                                return all_news
                        else:
                            print(f"[国际船舶网] 跳过非今天新闻 {date} (目标: {today})")

        print(f"[国际船舶网] 最终获取 {len(all_news)} 条新造船订单新闻")
        return all_news

    except Exception as e:
        print(f"[国际船舶网] 爬取错误: {e}")
        raise
    finally:
        driver.quit()


# 使用示例
if __name__ == "__main__":
    driver_path = None  # 使用 webdriver-manager 自动下载驱动

    try:
        # 示例1：获取今天的新闻（默认行为）
        recent_news = asyncio.run(scrape_eworldship_news(
            chrome_driver_path=driver_path,
            headless=True,
            filter_today=True,
            target_date=None,  # None表示获取今天/昨天的新闻
            max_news=None,  # 不限制数量
            extract_dates=True
        ))
        print(f"\n共获得{len(recent_news)} 条新闻")
        for i, news in enumerate(recent_news, 1):
            print(f"{i}. [{news['category']}] {news['title']} - {news['date']}")

    except Exception as e:
        print(f"程序执行失败: {e}")

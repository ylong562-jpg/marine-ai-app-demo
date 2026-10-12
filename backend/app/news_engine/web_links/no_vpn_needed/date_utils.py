"""
日期处理工具函数
提供统一的日期解析和验证功能
"""

import re
from datetime import datetime, timedelta


def get_today_str():
    """获取今日日期字符串 (YYYY-MM-DD)"""
    return datetime.now().strftime('%Y-%m-%d')


def get_yesterday_str():
    """获取昨日日期字符串 (YYYY-MM-DD)"""
    return (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')


def get_date_range(days=2):
    """
    获取日期范围（今天往前推n天）
    
    Parameters:
    days (int): 天数
    
    Returns:
    list: 日期字符串列表 [YYYY-MM-DD, ...]
    """
    dates = []
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        dates.append(date)
    return dates


def parse_date(date_text, default=None):
    """
    解析各种格式的日期文本
    
    Parameters:
    date_text (str): 日期文本
    default (str): 默认值
    
    Returns:
    str: YYYY-MM-DD 格式日期，或默认值
    """
    if not date_text:
        return default
    
    date_text = str(date_text).strip()
    
    # 尝试多种日期格式
    patterns = [
        # 2024-01-15
        (r'(\d{4})-(\d{1,2})-(\d{1,2})', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        # 2024/01/15
        (r'(\d{4})/(\d{1,2})/(\d{1,2})', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        # 2024年01月15日
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        # 01-15 (当前年份)
        (r'(\d{1,2})-(\d{1,2})', lambda m: f"{datetime.now().year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"),
        # 01/15 (当前年份)
        (r'(\d{1,2})/(\d{1,2})', lambda m: f"{datetime.now().year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"),
        # 15 Jan 2024
        (r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})', 
         lambda m: f"{m.group(3)}-{month_to_num(m.group(2))}-{m.group(1).zfill(2)}"),
        # Jan 15, 2024
        (r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
         lambda m: f"{m.group(3)}-{month_to_num(m.group(1))}-{m.group(2).zfill(2)}"),
    ]
    
    for pattern, formatter in patterns:
        match = re.search(pattern, date_text, re.IGNORECASE)
        if match:
            try:
                result = formatter(match)
                # 验证日期有效性
                datetime.strptime(result, '%Y-%m-%d')
                return result
            except:
                continue
    
    return default


def month_to_num(month_name):
    """将月份英文名称转换为数字"""
    months = {
        'january': '01', 'february': '02', 'march': '03', 'april': '04',
        'may': '05', 'june': '06', 'july': '07', 'august': '08',
        'september': '09', 'october': '10', 'november': '11', 'december': '12'
    }
    return months.get(month_name.lower(), '01')


def is_today_or_recent(date_str, days=2):
    """
    检查日期是否是今天或最近几天
    
    Parameters:
    date_str (str): 日期字符串 YYYY-MM-DD
    days (int): 往前推几天
    
    Returns:
    bool: 是否是今天或最近几天
    """
    if not date_str:
        return False
    
    try:
        valid_dates = get_date_range(days)
        return date_str in valid_dates
    except:
        return False


def extract_date_from_relative(text):
    """
    从相对时间文本中提取日期
    如："2小时前", "昨天", "刚刚"
    
    Parameters:
    text (str): 相对时间文本
    
    Returns:
    str: YYYY-MM-DD 格式日期
    """
    text = str(text).lower()
    today = get_today_str()
    yesterday = get_yesterday_str()
    
    # 刚刚、几分钟前、几小时前 -> 今天
    if any(word in text for word in ['刚刚', '分钟前', '小时前', 'ago', 'today']):
        return today
    
    # 昨天
    if '昨天' in text or 'yesterday' in text:
        return yesterday
    
    # 前天
    if '前天' in text or 'day before yesterday' in text:
        return (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    
    return None


def format_date_for_mysql(date_str):
    """
    确保日期格式符合MySQL要求
    
    Parameters:
    date_str (str): 日期字符串
    
    Returns:
    str: YYYY-MM-DD 格式
    """
    parsed = parse_date(date_str)
    if parsed:
        return parsed
    return get_today_str()  # 默认返回今天


# 测试
if __name__ == "__main__":
    print("日期工具测试:")
    print(f"今天: {get_today_str()}")
    print(f"昨天: {get_yesterday_str()}")
    print(f"最近3天: {get_date_range(3)}")
    
    test_dates = [
        "2024-03-15",
        "2024/03/15",
        "2024年03月15日",
        "03-15",
        "15 March 2024",
        "March 15, 2024",
        "2小时前",
        "昨天",
    ]
    
    print("\n日期解析测试:")
    for d in test_dates:
        print(f"  '{d}' -> {parse_date(d)}")

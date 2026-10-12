# -*- coding: utf-8 -*-
"""
货币换算服务：以人民币（CNY）为基准。

- USD/CNY 优先从 daily_data 表取最新 USDCHN 汇率
- 其他币种使用固定汇率兜底
"""
from datetime import date
from typing import List, Optional, Tuple

import mysql.connector

from app.core.config import get_settings

# 固定汇率：1 单位外币 = ? CNY（兜底）
# 这些比率会根据 USD/CNY 和 USD/外币的近似关系维护
_FIXED_RATES_TO_CNY = {
    "CNY": 1.0,
    "USD": 7.2,  # 兜底，会被 daily_data.USDCHN 覆盖
    "EUR": 7.8,
    "JPY": 0.048,
    "KRW": 0.0052,
    "GBP": 9.2,
    "HKD": 0.92,
    "AUD": 4.7,
    "CAD": 5.2,
}

# 常见币种识别词
_CURRENCY_ALIASES = {
    "CNY": ["人民币", "元", "¥", "RMB"],
    "USD": ["美元", "$", "美金", "US$", "USD"],
    "EUR": ["欧元", "€", "欧"],
    "JPY": ["日元", "円", "¥"],
    "KRW": ["韩元", "₩", "韩币"],
    "GBP": ["英镑", "￡", "镑"],
    "HKD": ["港币", "港元", "HK$"],
    "AUD": ["澳元", "澳币"],
    "CAD": ["加元", "加币"],
}


def _build_alias_to_code() -> dict:
    mapping = {}
    for code, aliases in _CURRENCY_ALIASES.items():
        mapping[code] = code
        for alias in aliases:
            mapping[alias.lower()] = code
    return mapping


_ALIAS_TO_CODE = _build_alias_to_code()


def detect_currency(text: str) -> Optional[str]:
    """从文本中识别货币代码。优先匹配长别名，避免"美元"被"元"覆盖。"""
    if not text:
        return None
    text = str(text)
    lower = text.lower()
    # 按别名长度降序，优先匹配更具体的货币词
    for alias in sorted(_ALIAS_TO_CODE.keys(), key=lambda x: len(x), reverse=True):
        if alias in lower:
            return _ALIAS_TO_CODE[alias]
    return None


def _get_usd_cny_rate() -> Optional[float]:
    """从 daily_data 取最新 USDCHN 汇率。"""
    try:
        settings = get_settings()
        db_config = settings.NEWS_DB_CONFIG
        conn = mysql.connector.connect(**db_config)
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT USDCHN FROM daily_data WHERE USDCHN IS NOT NULL ORDER BY date DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row and row.get("USDCHN"):
                return float(row["USDCHN"])
        finally:
            cur.close()
            conn.close()
    except Exception:
        pass
    return None


def get_rate_to_cny(currency_code: str) -> Optional[float]:
    """获取 1 单位外币对应的人民币数额。"""
    code = currency_code.upper() if currency_code else ""
    if code == "USD":
        usd_cny = _get_usd_cny_rate()
        if usd_cny:
            return usd_cny
    return _FIXED_RATES_TO_CNY.get(code)


def _multipliers_from_text(text: str) -> List[float]:
    """从文本中提取数量级倍数（支持中文万/亿/百万/千和英文 m/million/bn/billion）。"""
    import re

    multipliers: List[float] = []
    text_lower = str(text).lower()

    # 英文数量级：thousand / million / billion
    # 使用 '(?:\d)\s*(...)(?![a-zA-Z])' 确保 k/m/b 紧跟在数字后，避免误匹配 "many" 等词
    if re.search(r"(?:\d)\s*k(?![a-zA-Z])", text_lower):
        multipliers.append(1_000.0)
    if re.search(r"(?:\d)\s*(m|mln|million)(?![a-zA-Z])", text_lower):
        multipliers.append(1_000_000.0)
    if re.search(r"(?:\d)\s*(b|bn|billion)(?![a-zA-Z])", text_lower):
        multipliers.append(1_000_000_000.0)

    # 中文数量级：按从大到小只取第一个，避免百万与万重复
    for unit, mult in [
        ("亿", 100_000_000.0),
        ("百万", 1_000_000.0),
        ("万", 10_000.0),
        ("千", 1_000.0),
    ]:
        if unit in text_lower:
            multipliers.append(mult)
            break

    return multipliers


def parse_amount(text: str, default_currency: Optional[str] = None) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """
    解析价格字符串。

    返回: (min_amount, max_amount, currency_code)
    - 如果是单一价格，min == max
    - 如果无法解析，返回 (None, None, None)
    - 若未检测到货币但提供了 default_currency，则使用该默认货币
    """
    if not text:
        return None, None, None

    import re

    text = str(text)
    s = text.strip().replace(",", "")
    currency = detect_currency(text)
    if not currency and default_currency:
        currency = default_currency.upper()

    # 去掉常见前缀词和货币符号，保留数字部分
    s_clean = re.sub(r"(单船约|约|约合|大约|近|超|达|至|为|每股|每天|每日|per\s*day|/日|/天)", "", s, flags=re.IGNORECASE)
    s_clean = re.sub(r"(US\$|HK\$|A\$|C\$|\$|€|£|¥|₩)", "", s_clean)
    s_clean = s_clean.replace("—", "-").replace("–", "-").replace("~", "-")

    # 尝试匹配数字范围，如 "3000-4000万美元"、"5000万-1亿美元"、"$25m-$50m" 或 "$52k-$60k"
    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*([万亿千百百万kKmMbBnN]*)\s*[-~～]\s*(\d+(?:\.\d+)?)\s*([万亿千百百万kKmMbBnN]*)",
        s_clean,
    )
    if range_match:
        min_amt = float(range_match.group(1))
        max_amt = float(range_match.group(3))
    else:
        # 如果检测到货币，优先找货币词附近的数字
        if currency:
            # 在原始文本中定位货币词位置
            lower = s.lower()
            currency_pos = None
            for alias in sorted(_ALIAS_TO_CODE.keys(), key=lambda x: len(x), reverse=True):
                pos = lower.find(alias)
                if pos != -1:
                    currency_pos = pos
                    break

            if currency_pos is not None:
                # 取货币词附近（前后 30 字符）的文本进行数字匹配
                window_start = max(0, currency_pos - 30)
                window_end = min(len(s_clean), currency_pos + 40)
                window = s_clean[window_start:window_end]
                nums = re.findall(r"(\d+(?:\.\d+)?)", window)
                if nums:
                    # 优先选最靠近货币词的数字
                    best = min(nums, key=lambda n: abs(window.find(n) - (currency_pos - window_start)))
                    min_amt = max_amt = float(best)
                else:
                    nums = re.findall(r"(\d+(?:\.\d+)?)", s_clean)
                    if not nums:
                        return None, None, currency
                    min_amt = max_amt = float(nums[0])
            else:
                nums = re.findall(r"(\d+(?:\.\d+)?)", s_clean)
                if not nums:
                    return None, None, currency
                min_amt = max_amt = float(nums[0])
        else:
            nums = re.findall(r"(\d+(?:\.\d+)?)", s_clean)
            if not nums:
                return None, None, None
            min_amt = max_amt = float(nums[0])

    def _multiplier_from_unit(unit_text: str) -> float:
        text = (unit_text or "").lower()
        if "亿" in text:
            return 100_000_000
        if "b" in text:
            return 1_000_000_000
        if "百万" in text or "m" in text:
            return 1_000_000
        if "万" in text:
            return 10_000
        if "千" in text or "k" in text:
            return 1_000
        return 1.0

    if range_match:
        left_unit = range_match.group(2)
        right_unit = range_match.group(4)
        # 如果区间一端缺单位，继承另一端单位
        effective_unit = left_unit or right_unit
        min_amt *= _multiplier_from_unit(left_unit or effective_unit)
        max_amt *= _multiplier_from_unit(right_unit or effective_unit)
        # 此时中英文单位已由 _multiplier_from_unit 统一处理，无需额外乘数
    else:
        # 处理数量级单位：基于 best 数字位置附近检测，避免远处单位干扰
        if currency and currency_pos is not None:
            best_pos_in_window = window.find(best)
            best_num_pos = window_start + (best_pos_in_window if best_pos_in_window != -1 else 0)
        else:
            best_str = str(min_amt)
            best_num_pos = s_clean.find(best_str)
            if best_num_pos == -1:
                best_num_pos = 0

        unit_window = s_clean[max(0, best_num_pos - 12) : best_num_pos + 15]
        unit_multiplier = 1.0
        for mult in _multipliers_from_text(unit_window):
            unit_multiplier *= mult
        min_amt *= unit_multiplier
        max_amt *= unit_multiplier

    return min_amt, max_amt, currency


def convert_to_cny(text: str, default_currency: Optional[str] = None) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """
    将价格字符串换算为人民币（万元）。

    返回: (min_cny, max_cny, original_currency_code)
    - default_currency: 当文本中未识别到货币时使用的默认货币代码（如 "USD"）
    """
    min_amt, max_amt, currency = parse_amount(text, default_currency=default_currency)
    if min_amt is None or not currency:
        return None, None, currency

    rate = get_rate_to_cny(currency)
    if rate is None:
        return None, None, currency

    # 结果以 万元 为单位
    min_cny = min_amt * rate / 10_000
    max_cny = max_amt * rate / 10_000
    return min_cny, max_cny, currency


def format_cny_range(min_cny: Optional[float], max_cny: Optional[float]) -> Optional[str]:
    """格式化人民币金额为字符串，区间用 '-' 连接。"""
    if min_cny is None:
        return None
    if max_cny is None or abs(min_cny - max_cny) < 1e-6:
        return f"{min_cny:,.2f}"
    return f"{min_cny:,.2f}-{max_cny:,.2f}"

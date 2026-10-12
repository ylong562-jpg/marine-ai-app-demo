# -*- coding: utf-8 -*-
"""
航运指数报告生成服务。

为 ShippingIndexPage 提供两类导出能力：
1. 最新数据卡片图（HTML -> Playwright 截图 PNG）
2. 上周数据 HTML 报告（含 overview 表格、matplotlib 折线图、AI/规则走势分析）

逻辑参考 /tmp/py_extract/PythonProject 下的 picture_single_page.py 与 weekly_data.py，
但仅复用思路，不引用参考项目中的任何文件。
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import Any, Optional

import mysql.connector
import pandas as pd
from playwright.async_api import async_playwright

# matplotlib 配置（中文字体兜底）
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib import font_manager

# 强制重新扫描系统字体，确保 www 用户也能识别新安装的 Noto CJK 字体
font_manager._load_fontmanager(try_read_cache=False)

CJK_FONTS = ["Noto Sans CJK SC", "Droid Sans", "WenQuanYi Micro Hei", "SimHei", "Microsoft YaHei"]
plt.rcParams["font.sans-serif"] = CJK_FONTS
plt.rcParams["axes.unicode_minus"] = False

# 数据库配置（与 shipping_index.py 保持一致）
CRAWLER_DB_CONFIG = {
    "host": "your-db-host.example.com",
    "port": 3306,
    "user": "your-db-user",
    "password": "your-db-password",
    "database": "Time_Series",
}

DAILY_DATA_TABLE = "daily_data"

# ============================================================================
# 1. 通用数据库与数据工具
# ============================================================================


def _get_connection():
    return mysql.connector.connect(**CRAWLER_DB_CONFIG)


def _get_numeric_columns(conn) -> list[str]:
    cursor = conn.cursor()
    try:
        cursor.execute(f"DESCRIBE {DAILY_DATA_TABLE}")
        return [row[0] for row in cursor.fetchall() if row[0].lower() != "date"]
    finally:
        cursor.close()


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _format_number(value: float) -> str:
    """根据数量级格式化数值，避免过长。"""
    if value is None or pd.isna(value):
        return "-"
    abs_v = abs(value)
    if abs_v >= 10_000:
        return f"{value:,.0f}"
    if abs_v >= 100:
        return f"{value:,.2f}"
    if abs_v >= 1:
        return f"{value:.3f}"
    return f"{value:.4f}"


def _get_last_week_dates(reference: datetime | None = None) -> tuple[datetime, datetime]:
    """返回上周一到周五的日期。"""
    today = reference or datetime.now()
    weekday = today.weekday()
    last_monday = today - timedelta(days=weekday + 7)
    last_friday = last_monday + timedelta(days=4)
    return last_monday.replace(hour=0, minute=0, second=0, microsecond=0), last_friday.replace(
        hour=0, minute=0, second=0, microsecond=0
    )


# ============================================================================
# 2. 最新数据图片
# ============================================================================

# 数据集定义（参考 all_data.py）
_LATEST_DATASETS: dict[str, dict[str, str]] = {
    "main_data": {
        "BDI": "BDI",
        "CDI": "CDI",
        "CCFI": "CCFI",
        "XH.PDCI": "XH_PDCI",
        "BDTI": "BDTI",
        "中国进口原油<br>运价指数": "CTFI",
        "BLNG": "BLNG",
        "BLPG": "BLPG",
        "USD Index": "USD_INDEX",
        "USDCHN": "USDCHN",
        "USDJPY": "USDJPY",
        "380燃油指数<br>Singapore": "IFO_380cst_Bunker_Prices",
        "低硫燃油指数<br>Singapore": "VLSFO_Bunker_Prices",
        "高低硫燃油<br>价差": "S_Spread_data",
        "3m EURIBOR": "SHIBOR_3M",
        "10年SOFR<br>掉期利率": "10Y_SOFR_swap_rate",
        "中国碳配额": "CEA_RMB",
        "欧洲碳配额": "EUA_EUR",
    },
    "baltic_data": {
        "BDI": "BDI",
        "BCI": "BCI",
        "BPI": "BPI",
        "BSI": "BSI",
        "BHSI": "BHSI",
        "CCFI": "CCFI",
        "SCFI": "SCFI",
        "XH.PDCI": "XH_PDCI",
        "BDTI": "BDTI",
        "BCTI": "BCTI",
        "BLNG": "BLNG",
        "BLPG": "BLPG",
    },
    "USD_Index": {
        "LME Gold": "Gold_Price_LME_USD",
        "WTI Oil": "Oil_Price_WTI_USD",
        "Gold/Oil ratio": "Gold_to_oil_ratio",
        "IFO 380 cst<br>Singapore": "IFO_380cst_Bunker_Prices",
        "VLSFO<br>Singapore": "VLSFO_Bunker_Prices",
        "Sulphur Spread": "S_Spread_data",
        "USD Index": "USD_INDEX",
        "USDCHN": "USDCHN",
        "USDJPY": "USDJPY",
    },
    "bunker_and_steel_data": {
        "MGO": "MGO",
        "MEOH<br>常规甲醇": "MEOH",
        "MEoH-<br>MGOe": "MEoH_MGOe",
        "MEoH-<br>VLSFOe": "MEoH_VLSFOe",
        "green MEOH<br>绿色甲醇": "green MEOH绿色甲醇",
        "green MEOH<br>MGOe": "green MEOH-MGOe",
        "路透CRB Index": "CRB_COMMODITY_INDEX",
        "62% Fe Iron Ore CFR CN": "iron_ore",
        "S&P GSCI": "SPGSCI",
        "上海<br>10MM<br>船板": "SH_SHIP_PLATE_10MM",
        "广州<br>10MM<br>船板": "GZ_SHIP_PLATE_10MM",
        "南京<br>10MM<br>船板": "NJ_SHIP_PLATE_10MM",
    },
    "Yield_data": {
        "中国进口原油运价指数": "CTFI",
        "中国沿海散货船日租金指数": "CDI",
        "O/N SHIBOR": "SHIBOR_ON",
        "SOFR": "SOFR",
        "1m SHIBOR": "SHIBOR_1M",
        "1m Term SOFR": "CME_TERM_SOFR_1_MON",
        "3m SHIBOR": "SHIBOR_3M",
        "3m Term SOFR": "CME_TERM_SOFR_3_MONS",
        "6m SHIBOR": "SHIBOR_6M",
        "6m Term SOFR": "CME_TERM_SOFR_6_MONS",
        "12m SHIBOR": "SHIBOR_1Y",
        "12m Term SOFR": "CME_TERM_SOFR_12_MONS",
        "1年期国债收益率": "CN_1Y_BOND_YIELD",
        "1Y Treasury Yield": "US_1Y_BOND_YIELD",
        "3年期国债收益率": "CN_3Y_BOND_YIELD",
        "3Y Treasury Yield": "US_3Y_BOND_YIELD",
        "5年期国债收益率": "CN_5Y_BOND_YIELD",
        "5Y Treasury Yield": "US_5Y_BOND_YIELD",
        "7年期国债收益率": "CN_7Y_BOND_YIELD",
        "7Y Treasury Yield": "US_7Y_BOND_YIELD",
        "10年期国债收益率": "CN_10Y_BOND_YIELD",
        "10Y Treasury Yield": "US_10Y_BOND_YIELD",
        "30年期国债收益率": "CN_30Y_BOND_YIELD",
        "30Y Treasury Yield": "US_30Y_BOND_YIELD",
        "国债收益差<br>(10年-1年)": "CN_Yield_Spread_data",
        "Yield Spread<br>(10Y-1Y)": "US_Yield_Spread_data",
        "LPR 1Y": "LPR1Y",
        "1Y SOFR swap rate": "1Y_SOFR_swap_rate",
        "LPR 5Y": "LPR5Y",
        "3Y SOFR swap rate": "3Y_SOFR_swap_rate",
        "1w EURIBOR": "Euribor_1w",
        "5Y SOFR swap rate": "5Y_SOFR_swap_rate",
        "1m EURIBOR": "Euribor_1m",
        "7Y SOFR swap rate": "7Y_SOFR_swap_rate",
        "3m EURIBOR": "Euribor_3m",
        "10Y SOFR swap rate": "10Y_SOFR_swap_rate",
        "6m EURIBOR": "Euribor_6m",
        "15Y SOFR swap rate": "15Y_SOFR_swap_rate",
        "1y EURIBOR": "Euribor_1y",
        "30Y SOFR swap rate": "30Y_SOFR_swap_rate",
        "CEA中国碳价": "CEA_RMB",
        "EUA欧洲碳价": "EUA_EUR",
        "TJEA天津碳价": "TJCE_RMB",
        "KRBN 全球碳策略ETF": "KRBN",
        "东方财富航运港口指数": "EM_SHIPPING_PORT",
        "道琼斯美国海运<br>交通指数": "DJ_US_MARINE_TRANS_INDEX",
        "SCFIS欧线主连 ECM": "SCFIS_EUR_ECM",
        "SCFIS欧线次主连 ECS": "SCFIS_EUR_ECS",
    },
}

# 需要剥离的单位后缀
_VALUE_SUFFIXES = [" USD/t", " USD/b", " USD/Oz", " RMB/t"]

# 需要以百分比形式展示中间数值的列（数据库中存的是小数，如 0.0364）
_PERCENTAGE_COLUMNS = {
    "SOFR",
    "SHIBOR_ON",
    "SHIBOR_1M",
    "SHIBOR_3M",
    "SHIBOR_6M",
    "SHIBOR_1Y",
    "CME_TERM_SOFR_1_MON",
    "CME_TERM_SOFR_3_MONS",
    "CME_TERM_SOFR_6_MONS",
    "CME_TERM_SOFR_12_MONS",
    "CN_1Y_BOND_YIELD",
    "CN_3Y_BOND_YIELD",
    "CN_5Y_BOND_YIELD",
    "CN_7Y_BOND_YIELD",
    "CN_10Y_BOND_YIELD",
    "CN_30Y_BOND_YIELD",
    "US_1Y_BOND_YIELD",
    "US_3Y_BOND_YIELD",
    "US_5Y_BOND_YIELD",
    "US_7Y_BOND_YIELD",
    "US_10Y_BOND_YIELD",
    "US_30Y_BOND_YIELD",
    "1Y_SOFR_swap_rate",
    "3Y_SOFR_swap_rate",
    "5Y_SOFR_swap_rate",
    "7Y_SOFR_swap_rate",
    "10Y_SOFR_swap_rate",
    "15Y_SOFR_swap_rate",
    "30Y_SOFR_swap_rate",
    "Euribor_1w",
    "Euribor_1m",
    "Euribor_3m",
    "Euribor_6m",
    "Euribor_1y",
    "LPR1Y",
    "LPR5Y",
}


def _fetch_latest_data() -> tuple[dict[str, Optional[float]], dict[str, Optional[float]], str]:
    """
    返回：
      - raw_latest: {col: raw_value_float}
      - rates: {col: rate_fraction or None}
      - latest_date: str
    """
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        numeric_cols = _get_numeric_columns(conn)
        # 取最近 365 天，确保低频指标（如 XH.PDCI）也能拿到最新两个非零值
        start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        cursor.execute(
            f"SELECT date, {', '.join(numeric_cols)} FROM {DAILY_DATA_TABLE} "
            "WHERE date >= %s ORDER BY date DESC LIMIT 365",
            (start,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise ValueError("没有查询到数据")

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        latest_date = df["date"].max().strftime("%Y-%m-%d")

        # 对每个列取最近两个非零值
        raw_latest: dict[str, float] = {}
        raw_last: dict[str, float] = {}
        for col in numeric_cols:
            series = df[["date", col]].dropna()
            series = series[series[col] != 0].sort_values("date", ascending=False)
            if len(series) >= 1:
                raw_latest[col] = float(series.iloc[0][col])
            if len(series) >= 2:
                raw_last[col] = float(series.iloc[1][col])

        # 派生指标
        def _derive(latest_dict: dict, last_dict: dict):
            def get(key):
                return latest_dict.get(key)

            def get_last(key):
                return last_dict.get(key)

            if get("VLSFO_Bunker_Prices") is not None and get("IFO_380cst_Bunker_Prices") is not None:
                latest_dict["S_Spread_data"] = get("VLSFO_Bunker_Prices") - get("IFO_380cst_Bunker_Prices")
                last_dict["S_Spread_data"] = get_last("VLSFO_Bunker_Prices") - get_last("IFO_380cst_Bunker_Prices")
            if get("Gold_Price_LME_USD") is not None and get("Oil_Price_WTI_USD") is not None:
                latest_dict["Gold_to_oil_ratio"] = get("Gold_Price_LME_USD") / get("Oil_Price_WTI_USD")
                last_dict["Gold_to_oil_ratio"] = get_last("Gold_Price_LME_USD") / get_last("Oil_Price_WTI_USD")
            if (
                get("CN_10Y_BOND_YIELD") is not None
                and get("CN_1Y_BOND_YIELD") is not None
            ):
                latest_dict["CN_Yield_Spread_data"] = (get("CN_10Y_BOND_YIELD") - get("CN_1Y_BOND_YIELD")) * 10000
                last_dict["CN_Yield_Spread_data"] = (get_last("CN_10Y_BOND_YIELD") - get_last("CN_1Y_BOND_YIELD")) * 10000
            if (
                get("US_10Y_BOND_YIELD") is not None
                and get("US_1Y_BOND_YIELD") is not None
            ):
                latest_dict["US_Yield_Spread_data"] = (get("US_10Y_BOND_YIELD") - get("US_1Y_BOND_YIELD")) * 10000
                last_dict["US_Yield_Spread_data"] = (get_last("US_10Y_BOND_YIELD") - get_last("US_1Y_BOND_YIELD")) * 10000
            # 参考中的绿色甲醇硬编码值
            latest_dict["green MEOH绿色甲醇"] = 1200.0
            latest_dict["green MEOH-MGOe"] = 2400.0
            last_dict["green MEOH绿色甲醇"] = 1200.0
            last_dict["green MEOH-MGOe"] = 2400.0

        _derive(raw_latest, raw_last)

        rates: dict[str, Optional[float]] = {}
        for col in set(raw_latest.keys()) | set(raw_last.keys()):
            if raw_last.get(col) not in (None, 0):
                rates[col] = raw_latest.get(col, 0) / raw_last[col] - 1
            else:
                rates[col] = None
        # 绿色甲醇硬编码涨幅为 0
        rates.setdefault("green MEOH绿色甲醇", 0.0)
        rates.setdefault("green MEOH-MGOe", 0.0)

        return raw_latest, rates, latest_date
    finally:
        cursor.close()
        conn.close()


def _gradient_color(rate: Optional[float]) -> str:
    if rate is None or rate == 0:
        return "#8B8D8B"
    if rate > 0:
        return "#E04646" if abs(rate) <= 0.05 else "#992931"
    return "#4DB091" if abs(rate) <= 0.05 else "#38544A"


def _calculate_name_font_ratio(name_text: str) -> float:
    text_length = len(name_text)
    base_ratio = 1.0
    min_ratio = 0.6
    max_length = 20
    if text_length <= 3:
        font_ratio = base_ratio
    else:
        # 衰减速度减少到原先的 0.7 倍，使字数较多的名称字体更大一些
        font_ratio = base_ratio - (base_ratio - min_ratio) / (max_length - 3) * (text_length - 3) * 0.7
        font_ratio = max(font_ratio, min_ratio)
    base_font_size = 1.4
    avg_char_width_ratio = 0.6
    estimated_total_width = text_length * (base_font_size * font_ratio) * avg_char_width_ratio
    max_desired_width = 7
    if estimated_total_width > max_desired_width:
        calibrated = max_desired_width / (text_length * base_font_size * avg_char_width_ratio)
        font_ratio = min(font_ratio, calibrated)
        font_ratio = max(font_ratio, min_ratio)
    return round(font_ratio, 2)


def _process_name_text(text: str) -> str:
    font_ratio = _calculate_name_font_ratio(text)
    base_font_size = 1.4
    chinese_base_scale = 0.8
    final_font_size = base_font_size * font_ratio
    chinese_font_size = base_font_size * font_ratio * chinese_base_scale
    processed = re.sub(
        r"([\u4e00-\u9fa5])",
        lambda m: f'<span style="font-size: {chinese_font_size}rem;">{m.group(1)}</span>',
        text,
    )
    return f'<span style="font-size: {final_font_size}rem;">{processed}</span>'


def _strip_suffix(value_text: str) -> str:
    if value_text.startswith("abt. "):
        value_text = value_text[5:]
    for suffix in _VALUE_SUFFIXES:
        if value_text.endswith(suffix):
            value_text = value_text[: -len(suffix)]
            break
    return value_text


def _format_latest_value(col: str, value: Optional[float]) -> str:
    """格式化单张卡片中间的主数值。百分比列从小数转为百分数并加 % 后缀。"""
    if value is None or pd.isna(value):
        return "-"
    if col in _PERCENTAGE_COLUMNS:
        return f"{value * 100:.4f}%"
    return _format_number(value)


def _build_latest_html(raw_latest: dict, rates: dict, latest_date: str) -> str:
    sections = [
        {"title": "数据速览", "datasets": ["main_data"], "columns": 3},
        {
            "title": "更多数据",
            "datasets": ["baltic_data", "USD_Index", "bunker_and_steel_data"],
            "columns": 3,
        },
        {"title": None, "datasets": ["Yield_data"], "columns": 2},
    ]

    html_parts = [
        """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html { font-size: 30px; }
    body {
        font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Droid Sans", sans-serif;
        background: #FFFFFF;
        max-width: 900px; width: 100%; margin: 0 auto; padding: 40px;
        -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
    }
    .section { margin-bottom: 80px; }
    .section:last-child { margin-bottom: 60px; }
    .title-container {
        display: flex; align-items: center; gap: 16px; margin-bottom: 40px;
    }
    .title-bar {
        width: 6px; height: 3rem;
        background: linear-gradient(to bottom, #992931, #38544A);
        border-radius: 3px;
    }
    .title-text {
        font-size: 3rem; font-weight: 300; color: #2C3E50;
        letter-spacing: 1px;
    }
    .date-text {
        font-size: 1.1rem; font-weight: 300; color: #5D6D7E; margin-left: auto; align-self: flex-end;
    }
    .grid-container-2col {
        display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;
        width: 100%; margin-bottom: 60px;
    }
    .grid-container-3col {
        display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
        width: 100%; margin-bottom: 60px;
    }
    .grid-item-large {
        position: relative; aspect-ratio: 1 / 1; border-radius: 16px;
        padding: 25px; border: none; box-shadow: 0 4px 12px rgba(0,0,0,0.08);
        display: grid; grid-template-rows: minmax(0, 1fr) auto minmax(0, 1fr);
        align-items: center; justify-items: center; text-align: center;
        overflow: hidden; row-gap: 12px;
    }
    .grid-item-small {
        position: relative; aspect-ratio: 3 / 2; border-radius: 16px;
        padding: 25px; border: none; box-shadow: 0 4px 12px rgba(0,0,0,0.08);
        display: grid; grid-template-rows: minmax(0, 1fr) auto minmax(0, 1fr);
        align-items: center; justify-items: center; text-align: center;
        overflow: hidden; row-gap: 12px;
    }
    .name-large, .name-small {
        grid-row: 1; align-self: end; font-weight: 100; line-height: 1.0;
        margin: 0; letter-spacing: 0.5px; width: 100%;
        word-wrap: break-word; overflow-wrap: break-word;
    }
    .value-large, .value-small {
        grid-row: 2; align-self: center; display: flex; justify-content: center; align-items: center;
        font-weight: 200; font-size: 1.5rem; line-height: 1.1; margin: 0;
        letter-spacing: -1px; font-variant-numeric: tabular-nums; width: 100%;
    }
    .rate-large, .rate-small {
        grid-row: 3; align-self: start; font-weight: 200; font-size: 1rem;
        line-height: 1.3; font-variant-numeric: tabular-nums; width: 100%;
    }
    .rate-badge {
        display: inline-block; padding: 5px 10px; border-radius: 6px;
        font-variant-numeric: tabular-nums; font-size: 1rem; font-weight: 200;
    }
</style>
</head>
<body>"""
    ]

    for section in sections:
        title = section["title"]
        if title:
            html_parts.append(
                f'<div class="section"><div class="title-container">'
                f'<div class="title-bar"></div>'
                f'<div class="title-text">{title}</div>'
                f'</div>'
            )
        else:
            html_parts.append('<div class="section">')

        for dataset_name in section["datasets"]:
            mapping = _LATEST_DATASETS.get(dataset_name, {})
            grid_class = f"grid-container-{section['columns']}col"
            item_class = "grid-item-large" if section["columns"] == 3 else "grid-item-small"
            name_class = "name-large" if section["columns"] == 3 else "name-small"
            value_class = "value-large" if section["columns"] == 3 else "value-small"
            rate_class = "rate-large" if section["columns"] == 3 else "rate-small"
            html_parts.append(f'<div class="{grid_class}">')
            for display_name, col in mapping.items():
                raw_value = raw_latest.get(col)
                if raw_value is None:
                    continue
                value_text = _format_latest_value(col, raw_value)
                value_text = _strip_suffix(value_text)
                rate = rates.get(col)
                bg_color = _gradient_color(rate)
                text_color = "#FAFAD2"
                rate_str = f"{(rate * 100):.2f}%" if rate is not None else "-"
                processed_name = _process_name_text(display_name)
                card = f"""<div class="{item_class}" style="background-color: {bg_color};">
                    <div class="{name_class}" style="color: {text_color};">{processed_name}</div>
                    <div class="{value_class}" style="color: {text_color};">{value_text}</div>
                    <div class="{rate_class}" style="color: {text_color};">
                        <span class="rate-badge" style="color: {text_color};">{rate_str}</span>
                    </div>
                </div>"""
                html_parts.append(card)
            html_parts.append("</div>")
        html_parts.append("</div>")

    html_parts.append("</body></html>")
    return "".join(html_parts)


async def _screenshot_html_to_png(html_content: str) -> bytes:
    """以固定 900px 宽度截取整页，避免高 DPR 缩放导致字体/排版被压缩。"""
    async with _SCREENSHOT_SEMAPHORE:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--force-color-profile=srgb",
                    "--hide-scrollbars",
                ],
            )
            try:
                context = await browser.new_context(
                    device_scale_factor=1,
                    viewport={"width": 900, "height": 1200},
                )
                page = await context.new_page()
                await page.set_content(html_content, wait_until="networkidle")
                await page.wait_for_timeout(1200)
                png_bytes = await page.screenshot(full_page=True, type="png")
            finally:
                await browser.close()
                _cleanup_old_playwright_tmp()
        return png_bytes


async def generate_latest_image() -> tuple[bytes, str]:
    """生成最新数据 PNG，返回 (bytes, filename)。"""
    loop = asyncio.get_event_loop()
    raw_latest, rates, latest_date = await loop.run_in_executor(None, _fetch_latest_data)
    html = _build_latest_html(raw_latest, rates, latest_date)
    png_bytes = await _screenshot_html_to_png(html)
    filename = f"航运数据最新_{latest_date.replace('-', '')}.png"
    return png_bytes, filename


# ============================================================================
# 3. 周报数据与图表
# ============================================================================

_WEEKLY_CATEGORIES: dict[str, Any] = {
    "波罗的海指数": {
        "description": "波罗的海交易所发布的国际航运运价指数",
        "subcategories": {
            "干散货运价": {
                "items": ["BDI", "BCI", "BPI", "BSI", "BHSI"],
                "description": "波罗的海干散货运价指数系列",
            },
            "油运指数": {
                "items": ["BDTI", "BCTI"],
                "description": "波罗的海原油及成品油运价指数",
            },
            "气体运输": {
                "items": ["BLNG", "BLPG"],
                "description": "波罗的海液化天然气及液化石油气运价指数",
            },
        },
    },
    "中国运价指数": {
        "items": ["CCFI", "SCFI", "CTFI"],
        "description": "中国出口集装箱运价指数(CCFI/SCFI)及中国进口原油运价指数(CTFI)，CCFI/SCFI 使用两周数据",
        "extended_days": {"CCFI": 14, "SCFI": 14},
    },
    "汇率": {
        "items": ["USD_INDEX", "USDCHN", "USDJPY"],
        "description": "主要货币汇率走势",
    },
    "大宗商品": {
        "description": "大宗商品价格指数",
        "subcategories": {
            "具体商品指数": {
                "items": ["Gold_Price_LME_USD", "Oil_Price_WTI_USD", "iron_ore"],
                "description": "黄金、原油、铁矿石等具体大宗商品价格",
            },
            "综合商品指数": {
                "items": ["CRB_COMMODITY_INDEX", "SPGSCI"],
                "description": "CRB商品指数、标普高盛商品指数等综合大宗商品指数",
            },
        },
    },
    "燃油价格": {
        "items": ["IFO_380cst_Bunker_Prices", "VLSFO_Bunker_Prices", "MGO"],
        "description": "船用燃油及低硫燃油价格",
    },
    "船板价格": {
        "items": ["SH_SHIP_PLATE_10MM", "GZ_SHIP_PLATE_10MM", "NJ_SHIP_PLATE_10MM"],
        "description": "主要城市船用钢板价格",
    },
    "利率": {
        "items": ["SOFR", "SHIBOR_3M", "US_10Y_BOND_YIELD", "CN_10Y_BOND_YIELD"],
        "description": "主要市场基准利率及国债收益率",
    },
}

_NAME_MAPPING: dict[str, str] = {
    "BDI": "波罗的海干散货指数",
    "BCI": "波罗的海海岬型指数",
    "BPI": "波罗的海巴拿马型指数",
    "BSI": "波罗的海超灵便型指数",
    "BHSI": "波罗的海灵便型指数",
    "CCFI": "中国出口集装箱运价指数",
    "SCFI": "上海出口集装箱运价指数",
    "XH_PDCI": "新华·泛亚航运内贸指数",
    "BDTI": "波罗的海原油运价指数",
    "BCTI": "波罗的海成品油运价指数",
    "CTFI": "中国进口原油运价指数",
    "BLNG": "波罗的海液化天然气运价指数",
    "BLPG": "波罗的海液化石油气运价指数",
    "USD_INDEX": "美元指数",
    "USDCHN": "美元兑人民币",
    "USDJPY": "美元兑日元",
    "Gold_Price_LME_USD": "LME黄金价格",
    "Oil_Price_WTI_USD": "WTI原油价格",
    "iron_ore": "铁矿石62% Fe CFR中国",
    "IFO_380cst_Bunker_Prices": "IFO 380cst 新加坡",
    "VLSFO_Bunker_Prices": "VLSFO 新加坡",
    "MGO": "MGO船用柴油",
    "SH_SHIP_PLATE_10MM": "上海10mm船板",
    "GZ_SHIP_PLATE_10MM": "广州10mm船板",
    "NJ_SHIP_PLATE_10MM": "南京10mm船板",
    "SOFR": "SOFR美元隔夜利率",
    "SHIBOR_3M": "SHIBOR 3个月",
    "US_10Y_BOND_YIELD": "美国10年期国债收益率",
    "CN_10Y_BOND_YIELD": "中国10年期国债收益率",
    "CEA_RMB": "中国碳配额价格",
    "EUA_EUR": "欧洲碳配额价格",
    "LPR1Y": "LPR 1年期",
    "LPR5Y": "LPR 5年期",
    "CDI": "中国沿海散货船日租金指数",
    "CRB_COMMODITY_INDEX": "CRB商品指数",
    "SPGSCI": "标普高盛商品指数",
}

_RATE_COLUMNS = {"SOFR", "SHIBOR_3M", "US_10Y_BOND_YIELD", "CN_10Y_BOND_YIELD"}


def _fetch_weekly_df() -> pd.DataFrame:
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        numeric_cols = _get_numeric_columns(conn)
        yesterday = datetime.now() - timedelta(days=1)
        max_days = 35
        start_date = yesterday - timedelta(days=max_days)
        cursor.execute(
            f"SELECT date, {', '.join(numeric_cols)} FROM {DAILY_DATA_TABLE} "
            "WHERE date <= %s AND date >= %s ORDER BY date DESC",
            (yesterday.strftime("%Y-%m-%d"), start_date.strftime("%Y-%m-%d")),
        )
        rows = cursor.fetchall()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return df
    finally:
        cursor.close()
        conn.close()


def _get_column_data(df: pd.DataFrame, column: str, days: int = 5, is_extended: bool = False):
    """与 weekly_data.py 逻辑一致。"""
    if column not in df.columns:
        return None
    result_df = df[["date", column]].copy()
    result_df[column] = pd.to_numeric(result_df[column], errors="coerce")
    valid_df = result_df[(result_df[column] != 0) & (result_df[column].notna())].copy()
    if len(valid_df) == 0:
        return None
    valid_df = valid_df.sort_values("date", ascending=False)

    if is_extended:
        latest_valid_date = valid_df["date"].max()
        start_date = latest_valid_date - timedelta(days=days - 1)
        result_df = result_df[(result_df["date"] >= start_date) & (result_df["date"] <= latest_valid_date)]
        result_df = result_df[(result_df[column] != 0) & (result_df[column].notna())]
        return result_df.sort_values("date").reset_index(drop=True) if len(result_df) > 0 else None
    else:
        if len(valid_df) < days:
            selected_data = valid_df.copy()
        else:
            selected_data = valid_df.head(days).copy()
        last_monday, _ = _get_last_week_dates()
        last_week_dates = pd.date_range(start=last_monday, periods=5, freq="D")
        selected_data = selected_data.sort_values("date").reset_index(drop=True)
        selected_values = selected_data[column].values[:5]
        selected_dates = last_week_dates[: len(selected_values)]
        return pd.DataFrame({"date": selected_dates, column: selected_values})


def _calculate_change_analysis(item_df: pd.DataFrame, column: str) -> Optional[dict[str, Any]]:
    if item_df is None or len(item_df) < 2:
        return None
    values = item_df[column]
    first_value = float(values.iloc[0])
    last_value = float(values.iloc[-1])
    if first_value == 0 or pd.isna(first_value) or pd.isna(last_value):
        return None
    total_change = last_value - first_value
    total_change_pct = (total_change / abs(first_value)) * 100
    if abs(total_change_pct) < 0.5:
        trend = "平稳"
        trend_desc = "基本持平"
    elif total_change_pct > 0:
        trend = "上涨"
        trend_desc = f"累计上涨 {abs(total_change_pct):.2f}%"
    else:
        trend = "下跌"
        trend_desc = f"累计下跌 {abs(total_change_pct):.2f}%"
    volatility = ((values.max() - values.min()) / abs(first_value)) * 100 if first_value != 0 else 0
    mid_index = len(values) // 2
    mid_value = values.iloc[mid_index]
    if last_value > mid_value:
        weekly_trend = "后半周走强"
    elif last_value < mid_value:
        weekly_trend = "后半周走弱"
    else:
        weekly_trend = "周内震荡"
    return {
        "column": column,
        "first_value": first_value,
        "last_value": last_value,
        "total_change": total_change,
        "total_change_pct": total_change_pct,
        "trend": trend,
        "trend_desc": trend_desc,
        "min_value": values.min(),
        "max_value": values.max(),
        "volatility": volatility,
        "weekly_trend": weekly_trend,
    }


def _rule_analysis_text(analysis: dict, name: str) -> str:
    if analysis is None:
        return f"本周{name}数据不足，无法进行分析。"
    is_rate = analysis.get("column") in _RATE_COLUMNS
    first_str = f"{analysis['first_value']*100:.4f}%" if is_rate else f"{analysis['first_value']:.2f}"
    last_str = f"{analysis['last_value']*100:.4f}%" if is_rate else f"{analysis['last_value']:.2f}"
    return (
        f"本周{name}{analysis['trend_desc']}，"
        f"从周初的{first_str}变动至周末的{last_str}，"
        f"周内走势相对{'平稳' if analysis['volatility'] <= 2 else '波动'}（振幅{analysis['volatility']:.2f}%），"
        f"{analysis['weekly_trend']}。"
    )


def _clean_ai_text(text: str) -> str:
    """清理 AI 生成的分析文本，去除结尾的'以上'收尾词。"""
    if not text:
        return text
    return re.sub(r'[。，、；：！？\.（）()\s]*以上[。，、；：！？\.（）()\s]*$', '', text).strip()


def _y_axis_range(data_min: float, data_max: float) -> tuple[float, float]:
    if data_min == data_max:
        margin = 0.1 if abs(data_max) < 1 else abs(data_max) * 0.02
        return data_min - margin, data_max + margin
    data_range = data_max - data_min
    data_mid = (data_max + data_min) / 2
    amplitude_pct = data_range / abs(data_mid) if data_mid != 0 else data_range
    margin = data_range * 0.15
    if abs(data_mid) < 1:
        unit = 0.01
    elif abs(data_mid) < 10:
        unit = 0.1
    elif abs(data_mid) < 100:
        unit = 1.0
    elif abs(data_mid) < 1000:
        unit = 10.0
    else:
        unit = 100.0
    target_fill_ratio = 0.70
    min_display_range = max(data_range / target_fill_ratio, unit)
    if data_range < min_display_range:
        expand = (min_display_range - data_range) / 2
        return data_min - expand, data_max + expand
    return data_min - margin, data_max + margin


def _line_chart_base64(plot_df: pd.DataFrame, title: str) -> Optional[str]:
    if plot_df is None or len(plot_df) < 2:
        return None
    value_col = plot_df.columns[1]
    dates = plot_df["date"].reset_index(drop=True)
    values = plot_df[value_col].reset_index(drop=True)
    first_val = float(values.iloc[0])
    last_val = float(values.iloc[-1])
    line_color = "#D9534F" if last_val > first_val else "#5CB85C" if last_val < first_val else "#777777"
    is_rate = value_col in _RATE_COLUMNS
    fmt = lambda v: f"{v*100:.4f}%" if is_rate else f"{v:.2f}"

    fig, ax = plt.subplots(figsize=(7.5, 4), dpi=120)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    ax.plot(dates, values, color=line_color, linewidth=1.8, zorder=3)
    ax.scatter(dates, values, color=line_color, s=25, zorder=4, edgecolors="white", linewidths=0.5)

    y_min, y_max = _y_axis_range(float(values.min()), float(values.max()))
    ax.set_ylim(y_min, y_max)
    ax.set_title(title, fontsize=13, fontweight="normal", pad=12, color="#333333")
    ax.set_xlabel("日期", fontsize=9, color="#666666")
    ax.set_ylabel("数值", fontsize=9, color="#666666")
    ax.tick_params(axis="both", which="major", labelsize=8, colors="#666666")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.set_xticks(dates)
    if is_rate:
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x*100:.4f}%"))

    ax.grid(True, axis="y", linestyle="-", alpha=0.15, color="#666666")
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#CCCCCC")
    ax.spines["bottom"].set_color("#CCCCCC")

    ax.text(dates.iloc[0], first_val, f" {fmt(first_val)} ", fontsize=8, ha="right", va="center", color="#888888")
    ax.text(dates.iloc[-1], last_val, f" {fmt(last_val)} ", fontsize=8, ha="left", va="center", color=line_color, fontweight="bold")

    max_idx = values.idxmax()
    min_idx = values.idxmin()
    if max_idx not in (0, len(values) - 1):
        ax.text(dates.loc[max_idx], values.max(), f" {fmt(values.max())} ", fontsize=7, ha="center", va="bottom", color="#D9534F")
    if min_idx not in (0, len(values) - 1):
        ax.text(dates.loc[min_idx], values.min(), f" {fmt(values.min())} ", fontsize=7, ha="center", va="top", color="#5CB85C")

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, dpi=120, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _align_indicator_data(indicator_df: pd.DataFrame, value_col: str, global_start, global_end):
    indicator_df = indicator_df.sort_values("date").reset_index(drop=True)
    valid = indicator_df[indicator_df[value_col].notna() & (indicator_df[value_col] != 0)]
    if len(valid) == 0:
        return None
    indicator_start = valid["date"].min()
    indicator_end = valid["date"].max()
    full_range = pd.date_range(start=global_start, end=global_end, freq="D")
    aligned = pd.DataFrame({"date": full_range})
    aligned = aligned.merge(indicator_df[["date", value_col]], on="date", how="left")
    aligned[value_col] = aligned[value_col].ffill().bfill()
    if aligned[value_col].isna().any():
        if aligned[value_col].notna().sum() >= 2:
            aligned[value_col] = aligned[value_col].interpolate(method="linear")
        else:
            return None
    aligned = aligned[aligned[value_col].notna()].copy()
    return aligned if len(aligned) >= 2 else None


def _composite_line_chart_base64(data_dict: dict[str, pd.DataFrame], title: str) -> Optional[str]:
    valid = {}
    global_starts = []
    global_ends = []
    for name, df in data_dict.items():
        if df is None or len(df) < 2:
            continue
        value_col = df.columns[1]
        vdf = df[df[value_col].notna() & (df[value_col] != 0)]
        if len(vdf) == 0:
            continue
        valid[name] = df
        global_starts.append(vdf["date"].min())
        global_ends.append(vdf["date"].max())
    if not valid:
        return None
    global_start = min(global_starts)
    global_end = max(global_ends)

    colors = ["#D9534F", "#5CB85C", "#337AB7", "#F0AD4E", "#6C757D", "#5BC0DE", "#8E44AD", "#E67E22"]

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=120)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    all_values = []
    for idx, (name, df) in enumerate(valid.items()):
        value_col = df.columns[1]
        aligned = _align_indicator_data(df, value_col, global_start, global_end)
        if aligned is None or len(aligned) < 2:
            continue
        dates = aligned["date"].reset_index(drop=True)
        values = aligned[value_col].reset_index(drop=True)
        all_values.extend(values.tolist())
        color = colors[idx % len(colors)]
        ax.plot(dates, values, color=color, linewidth=1.8, label=name, zorder=3)
        ax.scatter(dates, values, color=color, s=20, zorder=4, edgecolors="white", linewidths=0.5)

    if not all_values:
        plt.close(fig)
        return None

    y_min, y_max = _y_axis_range(min(all_values), max(all_values))
    ax.set_ylim(y_min, y_max)
    ax.set_title(title, fontsize=13, fontweight="normal", pad=12, color="#333333")
    ax.set_xlabel("日期", fontsize=9, color="#666666")
    ax.set_ylabel("数值", fontsize=9, color="#666666")
    ax.tick_params(axis="both", which="major", labelsize=8, colors="#666666")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.set_xticks(sorted(set(pd.concat([df["date"] for df in valid.values()]).tolist())))
    ax.grid(True, axis="y", linestyle="-", alpha=0.12, color="#666666")
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#DDDDDD")
    ax.spines["bottom"].set_color("#DDDDDD")
    ax.legend(loc="best", fontsize=8, frameon=True, fancybox=False, edgecolor="#CCCCCC", facecolor="white", framealpha=0.9)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, dpi=120, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


# ============================================================================
# 4. AI 分析（可选，失败回退规则文本）
# ============================================================================

_AI_SEMAPHORE = asyncio.Semaphore(5)
_SCREENSHOT_SEMAPHORE = asyncio.Semaphore(3)


def _cleanup_old_playwright_tmp(max_age_minutes: int = 30):
    """清理 /tmp 中过期的 Playwright/Chromium 临时目录，防止磁盘膨胀。"""
    import shutil
    import tempfile

    now = datetime.now().timestamp()
    max_age_seconds = max_age_minutes * 60
    tmpdir = tempfile.gettempdir()
    prefixes = ("playwright", ".org.chromium", "playwright-artifacts", "playwright_chromiumdev_profile")
    for name in os.listdir(tmpdir):
        if not name.startswith(prefixes):
            continue
        path = os.path.join(tmpdir, name)
        if not os.path.isdir(path):
            continue
        try:
            if now - os.stat(path).st_mtime > max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


def _call_qwen(prompt: str) -> Optional[str]:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return None
    try:
        import dashscope
        response = dashscope.Generation.call(
            model="qwen-max",
            prompt=prompt,
            api_key=api_key,
            max_tokens=300,
            temperature=0.7,
            result_format="text",
        )
        if response.status_code == 200:
            text = getattr(response.output, "text", "") if hasattr(response.output, "text") else response.output.get("text", "")
            return text.strip()
    except Exception:
        pass
    return None


def _build_category_prompt(category_name: str, indicators: list[tuple[str, pd.DataFrame]]) -> str:
    lines = [f"你是一位拥有20年经验的资深航运分析师，擅长基于数据的专业市场解读。请用30-100字中文总结本周{category_name}走势。", ""]
    for name, df in indicators:
        value_col = df.columns[1]
        vals = df[value_col]
        first, last = float(vals.iloc[0]), float(vals.iloc[-1])
        change_pct = ((last - first) / abs(first) * 100) if first else 0
        is_rate = value_col in _RATE_COLUMNS
        first_str = f"{first*100:.4f}%" if is_rate else f"{first:.2f}"
        last_str = f"{last*100:.4f}%" if is_rate else f"{last:.2f}"
        lines.append(f"{name}: 周初{first_str} -> 周末{last_str} ({change_pct:+.2f}%)")
    lines.append("")
    lines.append("要求：分析语风严谨、专业，避免使用过多语气词和口语化表达，可基于数据给出适度主观判断。只输出分析文本，不要标题、编号或额外解释。")
    return "\n".join(lines)


def _build_indicator_prompt(name: str, analysis: dict) -> str:
    is_rate = analysis.get("column") in _RATE_COLUMNS
    first_str = f"{analysis['first_value']*100:.4f}%" if is_rate else f"{analysis['first_value']:.2f}"
    last_str = f"{analysis['last_value']*100:.4f}%" if is_rate else f"{analysis['last_value']:.2f}"
    return (
        f"你是一位资深航运分析师。请用30-80字中文分析本周{name}走势。\n"
        f"数据：周初{first_str}，周末{last_str}，"
        f"{analysis['trend_desc']}，振幅{analysis['volatility']:.2f}%，{analysis['weekly_trend']}。\n"
        "要求：分析语风严谨、专业，避免使用过多语气词和口语化表达，可基于数据给出适度主观判断。只输出分析文本。"
    )


async def _ai_category_analysis(category_name: str, indicators: list[tuple[str, pd.DataFrame]]) -> Optional[str]:
    async with _AI_SEMAPHORE:
        prompt = _build_category_prompt(category_name, indicators)
        return await asyncio.get_event_loop().run_in_executor(None, _call_qwen, prompt)


async def _ai_indicator_analysis(name: str, analysis: dict) -> Optional[str]:
    async with _AI_SEMAPHORE:
        prompt = _build_indicator_prompt(name, analysis)
        return await asyncio.get_event_loop().run_in_executor(None, _call_qwen, prompt)


# ============================================================================
# 5. 周报 HTML 组装
# ============================================================================


def _overview_table(df: pd.DataFrame) -> str:
    overview_items = [
        ("波罗的海指数", "BDI"),
        ("波罗的海指数", "BCI"),
        ("波罗的海指数", "BPI"),
        ("波罗的海指数", "BSI"),
        ("波罗的海指数", "BHSI"),
        ("波罗的海指数", "BDTI"),
        ("波罗的海指数", "BCTI"),
        ("波罗的海指数", "BLNG"),
        ("波罗的海指数", "BLPG"),
        ("中国运价指数", "CCFI"),
        ("中国运价指数", "SCFI"),
        ("中国运价指数", "CTFI"),
        ("汇率", "USD_INDEX"),
        ("汇率", "USDCHN"),
        ("汇率", "USDJPY"),
        ("大宗商品", "Gold_Price_LME_USD"),
        ("大宗商品", "Oil_Price_WTI_USD"),
        ("大宗商品", "iron_ore"),
        ("大宗商品", "CRB_COMMODITY_INDEX"),
        ("大宗商品", "SPGSCI"),
        ("燃油价格", "IFO_380cst_Bunker_Prices"),
        ("燃油价格", "VLSFO_Bunker_Prices"),
        ("燃油价格", "MGO"),
        ("船板价格", "SH_SHIP_PLATE_10MM"),
        ("船板价格", "GZ_SHIP_PLATE_10MM"),
        ("船板价格", "NJ_SHIP_PLATE_10MM"),
        ("利率", "SOFR"),
        ("利率", "SHIBOR_3M"),
        ("利率", "US_10Y_BOND_YIELD"),
        ("利率", "CN_10Y_BOND_YIELD"),
    ]

    RED = "#E04B4B"
    GREEN = "#4DB091"
    GRAY = "#8B8D8B"

    # 分类视觉主题：左侧色带 + 淡色背景，便于快速识别同一大类
    _CAT_STYLES = {
        "波罗的海指数": {"border": "#5DADE2", "bg": "#EBF5FB"},
        "中国运价指数": {"border": "#EC7063", "bg": "#FDEDEC"},
        "汇率": {"border": "#F5B041", "bg": "#FEF9E7"},
        "大宗商品": {"border": "#AF7AC5", "bg": "#F5EEF8"},
        "燃油价格": {"border": "#5D6D7E", "bg": "#F2F3F4"},
        "船板价格": {"border": "#48C9B0", "bg": "#E8F8F5"},
        "利率": {"border": "#58D68D", "bg": "#EAFAF1"},
    }

    rows = []
    for cat, col in overview_items:
        if col not in df.columns:
            continue
        days = 14 if col in ("CCFI", "SCFI") else 5
        is_ext = col in ("CCFI", "SCFI")
        item_df = _get_column_data(df, col, days=days, is_extended=is_ext)
        if item_df is None or len(item_df) < 2:
            continue
        value_col = item_df.columns[1]
        vals = item_df[value_col]
        first_val, last_val = float(vals.iloc[0]), float(vals.iloc[-1])
        change = last_val - first_val
        change_pct = (change / abs(first_val)) * 100 if first_val != 0 else 0
        if change_pct > 0.5:
            trend = "up"
        elif change_pct < -0.5:
            trend = "down"
        else:
            trend = "flat"
        is_rate = col in _RATE_COLUMNS
        if is_rate:
            current_str = f"{last_val*100:.4f}%"
            change_str = f"{change*100:+.4f}%"
        elif abs(last_val) >= 1000:
            current_str = f"{last_val:,.0f}"
            change_str = f"{change:+,.2f}"
        elif abs(last_val) >= 10:
            current_str = f"{last_val:.2f}"
            change_str = f"{change:+,.2f}"
        else:
            current_str = f"{last_val:.3f}"
            change_str = f"{change:+.3f}"
        pct_str = f"{change_pct:+.2f}%"
        if trend == "up":
            color = RED
        elif trend == "down":
            color = GREEN
        else:
            color = GRAY
        display_name = _NAME_MAPPING.get(col, col)
        rows.append(
            {
                "cat": cat,
                "display_name": display_name,
                "current_str": current_str,
                "change_str": change_str,
                "pct_str": pct_str,
                "color": color,
            }
        )

    rows_html = []
    for row in rows:
        style = _CAT_STYLES.get(row["cat"], {"border": "#34495E", "bg": "#f8f9fa"})
        rows_html.append(
            f"<tr>"
            f"<td style='padding:8px 8px;border-bottom:1px solid #eee;text-align:center;color:#2C3E50;font-size:11px;"
            f"border-left:4px solid {style['border']};background:{style['bg']};line-height:1.4;'>"
            f"{row['display_name']}</td>"
            f"<td style='padding:8px 8px;border-bottom:1px solid #eee;text-align:center;font-family:Consolas,monospace;font-size:11px;white-space:nowrap;'>"
            f"{row['current_str']}</td>"
            f"<td style='padding:8px 8px;border-bottom:1px solid #eee;text-align:center;font-family:Consolas,monospace;"
            f"color:{row['color']};font-weight:600;font-size:11px;white-space:nowrap;'>{row['change_str']}</td>"
            f"<td style='padding:8px 8px;border-bottom:1px solid #eee;text-align:center;font-family:Consolas,monospace;"
            f"color:{row['color']};font-weight:600;font-size:11px;white-space:nowrap;'>{row['pct_str']}</td>"
            f"</tr>"
        )

    if not rows_html:
        return ""
    return (
        "<table style='width:100%;border-collapse:collapse;margin-top:12px;font-size:12px;'>"
        "<thead><tr style='color:#000;'>"
        "<th style='padding:10px 8px;text-align:center;font-size:12px;'>指标</th>"
        "<th style='padding:10px 8px;text-align:center;font-size:12px;'>最新值</th>"
        "<th style='padding:10px 8px;text-align:center;font-size:12px;'>涨跌</th>"
        "<th style='padding:10px 8px;text-align:center;font-size:12px;'>涨跌幅</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
        "<p style='text-align:right;font-size:12px;color:#95A5A6;margin-top:8px;'>"
        "注：深红/深绿表示涨跌幅超过±5%，数据基于近7日变化计算</p>"
    )



def _section_header(title: str, level: int = 2) -> str:
    if level == 1:
        return (
            f'<h1 style="font-size:24px;font-weight:700;color:#2C3E50;margin:32px 0 16px 0;padding-bottom:8px;'
            f'border-bottom:2px solid #E8E8E8;">{title}</h1>'
        )
    if level == 2:
        return (
            f'<h2 style="font-size:18px;font-weight:700;color:#34495E;margin:28px 0 12px 0;">'
            f'<span style="display:inline-block;background:#34495E;color:#fff;padding:6px 14px;border-radius:8px;">'
            f"{title}</span></h2>"
        )
    return (
        f'<h3 style="font-size:15px;font-weight:600;color:#5D6D7E;margin:18px 0 8px 0;">{title}</h3>'
    )


def _card(title: str, content: str) -> str:
    return (
        f'<section style="background:#fff;border:1px solid #E8E8E8;border-radius:12px;padding:16px 18px;'
        f'margin-bottom:60px;box-shadow:0 1px 3px rgba(0,0,0,0.04);">'
        f'<div style="font-size:15px;font-weight:700;color:#2C3E50;margin-bottom:10px;">{title}</div>'
        f"{content}</section>"
    )


def _category_card(title: str, content: str) -> str:
    """分类模块卡片：保留原 level-2 标签式标题，并扩大卡片间距。"""
    return (
        f'<section style="background:#fff;border:1px solid #E8E8E8;border-radius:12px;padding:16px 18px;'
        f'margin-bottom:60px;font-size:14px;box-shadow:0 1px 3px rgba(0,0,0,0.04);">'
        f'<h2 style="font-size:18px;font-weight:700;color:#34495E;margin:0 0 12px 0;">'
        f'<span style="display:inline-block;background:#34495E;color:#fff;padding:6px 14px;border-radius:8px;">{title}</span>'
        f'</h2>{content}</section>'
    )


def _img_tag(b64: str, alt: str = "") -> str:
    return (
        f'<div style="text-align:center;margin:14px 0;">'
        f'<img src="data:image/png;base64,{b64}" alt="{alt}" style="max-width:100%;border-radius:8px;">'
        f"</div>"
    )


async def generate_weekly_report(use_ai: bool = True) -> tuple[bytes, str]:
    """生成周报 HTML，返回 (bytes, filename)。"""
    loop = asyncio.get_event_loop()
    df = await loop.run_in_executor(None, _fetch_weekly_df)
    if df.empty:
        raise ValueError("没有足够数据生成周报")

    last_monday, last_friday = _get_last_week_dates()
    start_date = last_monday.strftime("%Y-%m-%d")
    end_date = last_friday.strftime("%Y-%m-%d")

    html_parts = [
        """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>航运市场数据周报 - {start} ~ {end}</title>
<style>
    body {{ max-width: 820px; margin: 0 auto; padding: 24px; font-family: "PingFang SC", "Microsoft YaHei", "Droid Sans", "Noto Sans CJK SC", sans-serif; background: #fafafa; color: #333; font-size: 14px; line-height: 1.7; }}
</style>
</head>
<body>""".format(start=start_date, end=end_date)
    ]

    html_parts.append(
        f'<table style="width:100%;border-collapse:collapse;background-color:#fff;border:1px solid #e8e8e8;margin-bottom:24px;">'
        f'<tr><td style="text-align:center;padding:28px 20px;">'
        f'<p style="font-size:28px;font-weight:bold;color:#2c3e50;margin:0 0 8px 0;letter-spacing:2px;text-align:center;">航运市场数据周报</p>'
        f'<p style="font-size:14px;color:#666;margin:0;text-align:center;">{start_date} ~ {end_date} | 聚焦全球航运市场动态</p>'
        f'</td></tr></table>'
    )

    # 市场概述
    html_parts.append(_section_header("市场概述", level=1))
    html_parts.append(
        '<h2 style="font-size:17px;font-weight:700;color:#2C3E50;margin:24px 0 10px 0;">数据总览</h2>'
    )
    html_parts.append(_overview_table(df))

    # 分类详情
    html_parts.append(_section_header("详细数据分析", level=1))

    for category_name, category_info in _WEEKLY_CATEGORIES.items():
        category_parts: list[str] = []
        desc = category_info.get("description", "")
        if desc:
            category_parts.append(f'<p style="font-size:13px;color:#7F8C8D;font-style:italic;margin-bottom:14px;">{desc}</p>')

        # 分类级 AI 分析占位符：最后会回填到分类标题/描述下方
        category_ai_placeholder = len(category_parts)
        category_parts.append("")

        # 收集本分类下所有指标，用于 AI 分类分析
        category_indicators: list[tuple[str, pd.DataFrame]] = []
        subcategories = category_info.get("subcategories")
        extended_days = category_info.get("extended_days", {})

        items_to_process = []
        if subcategories:
            for sub_name, sub_info in subcategories.items():
                category_parts.append(_section_header(sub_name, level=3))
                sub_desc = sub_info.get("description", "")
                if sub_desc:
                    category_parts.append(f'<p style="font-size:12px;color:#7F8C8D;font-style:italic;margin-bottom:10px;">{sub_desc}</p>')
                # 综合图
                sub_dict = {}
                for item in sub_info["items"]:
                    if item not in df.columns:
                        continue
                    days = extended_days.get(item, 5)
                    is_ext = item in extended_days
                    item_df = _get_column_data(df, item, days=days, is_extended=is_ext)
                    if item_df is not None and len(item_df) >= 2:
                        sub_dict[_NAME_MAPPING.get(item, item)] = item_df
                        items_to_process.append((item, item_df, sub_name))
                if sub_dict:
                    b64 = _composite_line_chart_base64(sub_dict, f"{sub_name}综合走势")
                    if b64:
                        category_parts.append(_img_tag(b64, f"{sub_name}综合走势"))
        else:
            items_list = category_info.get("items", [])
            for item in items_list:
                if item not in df.columns:
                    continue
                days = extended_days.get(item, 5)
                is_ext = item in extended_days
                item_df = _get_column_data(df, item, days=days, is_extended=is_ext)
                if item_df is not None and len(item_df) >= 2:
                    items_to_process.append((item, item_df, None))

        # 单指标图表与分析
        indicator_analysis_tasks = []
        for item, item_df, _ in items_to_process:
            display_name = _NAME_MAPPING.get(item, item)
            b64 = _line_chart_base64(item_df, display_name)
            if b64:
                category_parts.append(_img_tag(b64, display_name))
            analysis = _calculate_change_analysis(item_df, item)
            # 默认规则文本
            rule_text = _rule_analysis_text(analysis, display_name)
            if use_ai and analysis is not None:
                indicator_analysis_tasks.append((display_name, analysis, rule_text, len(category_parts)))
                category_parts.append("")  # placeholder
            else:
                category_parts.append(f'<div style="font-size:14px;color:#34495E;line-height:1.7;margin:8px 0;"><strong>【走势分析】</strong>{rule_text}</div>')
            if item_df is not None and len(item_df) >= 2:
                category_indicators.append((display_name, item_df))

        # 生成分类级 AI 分析，并回填到分类标题/描述下方
        category_ai_text = None
        if use_ai and category_indicators:
            category_ai_text = await _ai_category_analysis(category_name, category_indicators)
        if category_ai_text:
            category_parts[category_ai_placeholder] = (
                f'<p style="font-size:14px;line-height:1.8;color:#2C3E50;margin:10px 0;">&emsp;&emsp;{_clean_ai_text(category_ai_text)}</p>'
            )
        else:
            category_parts[category_ai_placeholder] = ""

        # 回填单指标 AI 分析
        if indicator_analysis_tasks:
            ai_results = await asyncio.gather(
                *[_ai_indicator_analysis(name, analysis) for name, analysis, _, _ in indicator_analysis_tasks],
                return_exceptions=True,
            )
            for (name, analysis, rule_text, placeholder_idx), ai_text in zip(indicator_analysis_tasks, ai_results):
                final_text = ai_text if isinstance(ai_text, str) and ai_text.strip() else rule_text
                category_parts[placeholder_idx] = (
                    f'<div style="font-size:14px;color:#34495E;line-height:1.7;margin:8px 0;"><strong>【走势分析】</strong>{_clean_ai_text(final_text)}</div>'
                )

        html_parts.append(_category_card(category_name, "".join(category_parts)))

    html_parts.append("</body></html>")
    html = "".join(html_parts)
    filename = f"航运数据周报_{start_date.replace('-', '')}_{end_date.replace('-', '')}.html"
    return html.encode("utf-8"), filename

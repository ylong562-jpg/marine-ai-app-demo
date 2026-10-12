# -*- coding: utf-8 -*-
"""
市场分析服务：从新造船订单、买卖船、租船三类新闻中提取交易价格数据，
统一换算为人民币（万元），并提供按日期、船型、船厂等维度的聚合。
"""
import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

# 旧表名，保留仅作兼容参考；新逻辑使用 _MARKET_TABLES 按类别路由
_MARKET_ANALYSIS_TABLE = "market_analysis_transactions"

# 按市场类别拆分到三张独立表
_MARKET_TABLES = {
    "新造船订单": "market_analysis_newbuilding",
    "买卖船": "market_analysis_sale",
    "租船": "market_analysis_charter",
}

def _get_market_table(category: str) -> str:
    if category not in _MARKET_TABLES:
        raise ValueError(f"不支持的市场类别: {category}")
    return _MARKET_TABLES[category]

import mysql.connector

from app.core.config import get_settings
from app.services.currency_service import convert_to_cny, format_cny_range

_CATEGORY_MAP = {
    "newbuilding": "新造船订单",
    "sale": "买卖船",
    "charter": "租船",
}

_PRICE_FIELDS = {
    "新造船订单": "price",
    "买卖船": "money",
    "租船": "money",
}


def _parse_json_loose(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text in ("null", "None", "NULL"):
            return None
        try:
            return json.loads(text)
        except Exception:
            # 尝试处理单引号等非标准 JSON
            try:
                return json.loads(text.replace("'", '"'))
            except Exception:
                return None
    return None


def _extract_transactions(data_obj: Any, category: str) -> List[Dict[str, Any]]:
    """从 data 对象中提取交易列表。"""
    if not data_obj:
        return []

    # 支持分组格式 [{"main_tag": ..., "data": ...}]
    if isinstance(data_obj, list):
        transactions = []
        for item in data_obj:
            if not isinstance(item, dict):
                continue
            # 分组格式：item.data 是内层 JSON 字符串或对象
            inner = item.get("data")
            if isinstance(inner, str):
                inner = _parse_json_loose(inner)
            if inner:
                transactions.extend(_extract_transactions(inner, category))
            # 列表项本身也可能是含 transactions 的字典
            elif item.get("transactions") is not None:
                transactions.extend(_extract_transactions(item, category))
        return transactions

    if isinstance(data_obj, dict):
        txs = data_obj.get("transactions")
        if isinstance(txs, list):
            return txs
        return [data_obj]

    return []


def _parse_quantity(qty: Any) -> Optional[int]:
    """从 quantity 字段解析出正整数数量。"""
    if qty is None:
        return None
    if isinstance(qty, bool):
        return None
    if isinstance(qty, int):
        return qty if qty > 0 else None
    if isinstance(qty, float):
        return int(qty) if qty > 0 else None
    if isinstance(qty, str):
        text = qty.strip().replace(",", "")
        if not text or text.lower() in ("null", "none"):
            return None
        # 支持 "4艘"、"4"、"4 ships" 等格式
        match = re.search(r"(\d+)", text)
        if match:
            val = int(match.group(1))
            return val if val > 0 else None
    return None


def _parse_duration_days(text: Any) -> Optional[int]:
    """从租船价格文本中提取租期天数，如 '1.57亿美元/440天' -> 440。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    match = re.search(r"/\s*([\d,]+)\s*(天|日|day|days)\b", s, re.IGNORECASE)
    if match:
        val = int(match.group(1).replace(",", ""))
        return val if val > 0 else None
    return None


def _normalize_price(
    price_text: Optional[Any], category: str, quantity: Optional[int] = None
) -> Dict[str, Any]:
    """解析并归一化价格；若提供数量，则换算为单艘船价格。"""
    result = {
        "original_price": price_text,
        "currency": None,
        "cny_min": None,
        "cny_max": None,
        "cny_display": None,
        "is_rate": category == "租船",
    }
    if price_text is None or price_text == "":
        return result

    min_cny, max_cny, currency = convert_to_cny(str(price_text), default_currency="USD")

    # 新造船/买卖船：若存在有效数量，换算为单艘船价格
    if quantity and quantity > 0 and category != "租船":
        if min_cny is not None:
            min_cny /= quantity
        if max_cny is not None:
            max_cny /= quantity

    # 租船：若价格文本包含 "/天数"，将总价换算为日租金
    if category == "租船":
        days = _parse_duration_days(price_text)
        if days and days > 0:
            if min_cny is not None:
                min_cny /= days
            if max_cny is not None:
                max_cny /= days

    result["currency"] = currency
    result["cny_min"] = min_cny
    result["cny_max"] = max_cny
    result["cny_display"] = format_cny_range(min_cny, max_cny)
    return result


# ==================== 船舶规格归一化 ====================

# 常见船型等级对应的国际通行吨位/箱位范围（用于原文未给出具体数值时）
# 顺序：更具体的规则放前面，优先匹配。
_SHIP_TYPE_SIZE_RANGES: List[Dict[str, Any]] = [
    # 散货船
    {"patterns": ["纽卡斯尔型", "newcastlemax"], "unit": "DWT", "low": 200_000, "high": 210_000},
    {"patterns": ["卡姆萨尔型", "kamsarmax"], "unit": "DWT", "low": 80_000, "high": 85_000},
    {"patterns": ["好望角型", "capesize"], "unit": "DWT", "low": 100_000, "high": 210_000},
    {"patterns": ["超大型矿砂船", "vloc"], "unit": "DWT", "low": 200_000, "high": 400_000},
    {"patterns": ["极超灵便型", "极限灵便型", "ultramax"], "unit": "DWT", "low": 60_000, "high": 65_000},
    {"patterns": ["超灵便型", "supramax"], "unit": "DWT", "low": 50_000, "high": 60_000},
    {"patterns": ["大灵便型", "handymax"], "unit": "DWT", "low": 35_000, "high": 60_000},
    {"patterns": ["灵便型", "handysize"], "unit": "DWT", "low": 10_000, "high": 40_000},
    {"patterns": ["巴拿马型散货船"], "unit": "DWT", "low": 60_000, "high": 80_000},
    # 油轮
    {"patterns": ["超大型原油船", "vlcc"], "unit": "DWT", "low": 200_000, "high": 320_000},
    {"patterns": ["苏伊士型", "suezmax"], "unit": "DWT", "low": 120_000, "high": 200_000},
    {"patterns": ["阿芙拉型", "aframax"], "unit": "DWT", "low": 80_000, "high": 120_000},
    {"patterns": ["远程2型成品油轮", "lr2"], "unit": "DWT", "low": 110_000, "high": 120_000},
    {"patterns": ["远程1型成品油轮", "lr1"], "unit": "DWT", "low": 60_000, "high": 75_000},
    {"patterns": ["中程成品油轮", "mr"], "unit": "DWT", "low": 30_000, "high": 55_000},
    {"patterns": ["小型成品油轮"], "unit": "DWT", "low": 10_000, "high": 25_000},
    # 集装箱船
    {"patterns": ["超大型集装箱船"], "unit": "TEU", "low": 14_500, "high": 24_000},
    {"patterns": ["新巴拿马型集装箱船"], "unit": "TEU", "low": 10_000, "high": 14_500},
    {"patterns": ["超巴拿马型集装箱船"], "unit": "TEU", "low": 5_000, "high": 10_000},
    {"patterns": ["巴拿马型集装箱船"], "unit": "TEU", "low": 3_000, "high": 5_000},
    {"patterns": ["次巴拿马型集装箱船"], "unit": "TEU", "low": 2_000, "high": 3_000},
    {"patterns": ["灵便型集装箱船"], "unit": "TEU", "low": 1_000, "high": 2_000},
    {"patterns": ["支线集装箱船"], "unit": "TEU", "low": 100, "high": 3_000},
]


def _detect_spec_unit(text: str) -> Optional[str]:
    """识别规格字符串中的单位，统一返回 DWT / TEU / CBM / CEU / None。"""
    if not text:
        return None
    s = str(text).lower()
    if any(k in s for k in ("teu", "箱")):
        return "TEU"
    if any(k in s for k in ("dwt", "载重吨")):
        return "DWT"
    if any(k in s for k in ("立方米", "cu m", "cbm")):
        return "CBM"
    if "车位" in s:
        return "CEU"
    # “万吨”“吨”在散货/油轮语境下视为 DWT；如果前面已识别到 TEU/CBM/CEU 则不会走到这里
    if "吨" in s or re.search(r"\bkt\b|\bkts\b", s):
        return "DWT"
    return None


def _parse_numbers_with_multipliers(text: str) -> Tuple[List[float], bool]:
    """从文本中提取所有数字（支持 k/千/万/m/百万），并返回是否为范围。"""
    nums: List[float] = []
    if not text:
        return nums, False
    raw = str(text)
    # 统一替换全角符号
    normalized = raw.replace("，", ",").replace("–", "-").replace("～", "-").replace("~", "-")
    pattern = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM千百万]?)")
    for m in pattern.finditer(normalized):
        val_str = m.group(1).replace(",", "")
        val = float(val_str)
        suffix = m.group(2)
        if suffix:
            s = suffix.lower()
            if s in ("k", "千"):
                val *= 1_000
            elif s == "万":
                val *= 10_000
            elif s in ("m", "百万"):
                val *= 1_000_000
        nums.append(val)
    is_range = False
    if len(nums) >= 2:
        # 简单判断两个数字之间是否出现范围连接符或“至/到”
        if re.search(r"\d\s*[-–~～]\s*\d", normalized) or "至" in normalized or "到" in normalized:
            is_range = True
    return nums, is_range


def _match_ship_type_range(ship_type: Optional[str], specs: Optional[str]) -> Optional[Dict[str, Any]]:
    """根据船型名或规格描述匹配国际通行吨位/箱位范围。"""
    text = f"{ship_type or ''} {specs or ''}".lower()
    if not text.strip():
        return None
    for rule in _SHIP_TYPE_SIZE_RANGES:
        for pat in rule["patterns"]:
            if pat.lower() in text:
                return rule
    return None


def _parse_specs(specs: Optional[str], ship_type: Optional[str]) -> Dict[str, Any]:
    """
    归一化规格字段：
    - 识别 DWT/TEU/CBM/CEU 等单位和数值；范围取中间值。
    - 若原文未给出具体数值，则按船型等级（巴拿马型、好望角型、VLCC 等）补国际通行范围，并取中间值。
    - 返回统一后的 specs 显示字符串、spec_value（中间值）和 spec_unit。
    """
    original_specs = (specs or "").strip()
    if not original_specs:
        # 船型本身可能就能推出范围
        rule = _match_ship_type_range(ship_type, None)
        if rule:
            mid = (rule["low"] + rule["high"]) / 2.0
            unit = rule["unit"]
            display = f"{ship_type}（{rule['low']}-{rule['high']} {unit}）"
            return {"specs": display, "spec_value": mid, "spec_unit": unit}
        return {"specs": None, "spec_value": None, "spec_unit": None}

    nums, is_range = _parse_numbers_with_multipliers(original_specs)
    unit = _detect_spec_unit(original_specs)
    display = original_specs
    value: Optional[float] = None

    if nums and unit:
        if is_range and len(nums) >= 2:
            low, high = nums[0], nums[1]
            value = (low + high) / 2.0
            if unit in ("DWT", "TEU"):
                display = f"{int(low)}-{int(high)} {unit}"
        else:
            value = nums[0]
            if unit in ("DWT", "TEU"):
                display = f"{int(value)} {unit}"

    # 无法识别具体数值/单位时，尝试按船型等级补范围
    if value is None:
        rule = _match_ship_type_range(ship_type, original_specs)
        if rule:
            value = (rule["low"] + rule["high"]) / 2.0
            unit = rule["unit"]
            display = f"{ship_type}（{rule['low']}-{rule['high']} {unit}）"

    return {"specs": display if display else original_specs, "spec_value": value, "spec_unit": unit}


def _parse_news_date(date_val: Any) -> Optional[str]:
    if date_val is None:
        return None
    if isinstance(date_val, (datetime, date)):
        return date_val.strftime("%Y-%m-%d")
    s = str(date_val).strip()
    if not s:
        return None
    return s[:10]


def _has_real_value(value: Any) -> bool:
    """判断字段是否包含有效值（过滤 null/None/空字符串等）。"""
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("null", "none"):
            return False
    return True


def _is_reliable_transaction(
    tx: Dict[str, Any],
    category: str,
    price_text: Any,
) -> bool:
    """
    判断交易是否为可靠消息。
    - 必须有具体船型
    - 必须有价格
    - 新造船订单还必须有船厂
    """
    if not _has_real_value(tx.get("ship_type")):
        return False
    if not _has_real_value(price_text):
        return False
    if category == "新造船订单" and not _has_real_value(tx.get("shipyard")):
        return False
    return True


def _transaction_to_record(
    news: Dict[str, Any],
    tx: Dict[str, Any],
    category: str,
) -> Optional[Dict[str, Any]]:
    """把单笔交易转换为市场分析记录。"""
    price_field = _PRICE_FIELDS.get(category)
    price_text = tx.get(price_field) if price_field else None

    # 租船：优先取 money，允许带 /日 /天 /per day
    if category == "租船" and not price_text:
        price_text = tx.get("money")

    # 必须为有船型、有价格、新造船有船厂的可靠消息
    if not _is_reliable_transaction(tx, category, price_text):
        return None

    quantity = _parse_quantity(tx.get("quantity"))
    price_info = _normalize_price(price_text, category, quantity)

    record = {
        "news_id": news.get("id"),
        "news_title": news.get("title", ""),
        "news_link": news.get("link", ""),
        "news_date": _parse_news_date(news.get("date")),
        "category": category,
        "category_key": [k for k, v in _CATEGORY_MAP.items() if v == category][0] if category in _CATEGORY_MAP.values() else None,
        "trade_type": tx.get("trade_type") or ("期租" if category == "租船" else None),
        "date": tx.get("date") or _parse_news_date(news.get("date")),
        "ship_type": tx.get("ship_type") or news.get("ship_type"),
        "quantity": tx.get("quantity"),
        **_parse_specs(
            tx.get("specs"),
            tx.get("ship_type") or news.get("ship_type"),
        ),
        "shipowner": tx.get("shipowner") if category == "新造船订单" else None,
        "shipyard": tx.get("shipyard") if category == "新造船订单" else None,
        "buyer": tx.get("buyer") if category == "买卖船" else None,
        "seller": tx.get("seller") if category == "买卖船" else None,
        "charterer": tx.get("charterer") if category == "租船" else None,
        "owner": tx.get("owner") if category == "租船" else None,
        "delivery": tx.get("delivery"),
        "power": tx.get("power"),
        "charter_type": tx.get("charter_type") if category == "租船" else None,
        "_raw_transaction": dict(tx),
        **price_info,
    }
    return record


def _record_price_value(record: Dict[str, Any]) -> Optional[float]:
    """取记录的价格代表值（区间取中点），用于统计分析。"""
    min_cny = record.get("cny_min")
    max_cny = record.get("cny_max")
    if min_cny is None:
        return None
    if max_cny is not None and max_cny != min_cny:
        return (min_cny + max_cny) / 2.0
    return float(min_cny)


def _price_bounds(category: str) -> Tuple[Optional[float], Optional[float]]:
    """按市场类别设定价格合理范围（万元），用于兜底过滤极端噪音。"""
    if category == "租船":
        # 租金按日计，VLCC 历史高位约 40 万美元/日 ≈ 270 万元/日
        return 0.5, 500.0
    # 新造船/买卖船按单船总价计（万元）
    return 50.0, 5_000_000.0


def _filter_price_outliers(records: List[Dict[str, Any]], min_samples: int = 10) -> List[Dict[str, Any]]:
    """
    综合统计方法（对数 MAD）与业务合理范围，去除价格异常值。
    """
    if not records:
        return records

    category = records[0].get("category", "")
    lower_bound, upper_bound = _price_bounds(category)

    # 第一步：业务兜底，过滤明显超出合理范围的极端值
    outlier_indices: set = set()
    for i, r in enumerate(records):
        p = _record_price_value(r)
        if p is None:
            continue
        if lower_bound is not None and p < lower_bound:
            outlier_indices.add(i)
        if upper_bound is not None and p > upper_bound:
            outlier_indices.add(i)

    # 第二步：对数 MAD 方法过滤相对异常值
    valid = [(i, _record_price_value(r)) for i, r in enumerate(records) if i not in outlier_indices]
    valid = [(i, p) for i, p in valid if p is not None and p > 0]

    if len(valid) >= min_samples:
        import statistics

        log_prices = [math.log10(p) for _, p in valid]
        median = statistics.median(log_prices)
        mad = statistics.median([abs(lp - median) for lp in log_prices]) or 1e-9
        # modified Z-score 阈值 3.5
        for idx, p in valid:
            log_p = math.log10(p)
            modified_z = 0.6745 * (log_p - median) / mad
            if abs(modified_z) > 3.5:
                outlier_indices.add(idx)

    if outlier_indices:
        logging.info(
            "市场分析去噪：类别 %s，原始 %d 条，去除 %d 条价格异常记录",
            category,
            len(records),
            len(outlier_indices),
        )

    return [r for i, r in enumerate(records) if i not in outlier_indices]


def _ensure_market_analysis_table(conn) -> None:
    """确保三张市场分析交易记录表均存在。"""
    base_sql = """
    CREATE TABLE IF NOT EXISTS `{table_name}` (
        `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
        `news_id` INT NOT NULL,
        `news_title` VARCHAR(500),
        `news_link` VARCHAR(500),
        `news_date` DATE,
        `news_summary` LONGTEXT,
        `source_platform` VARCHAR(100),
        `category_key` VARCHAR(50) NOT NULL,
        `date` VARCHAR(50),
        `ship_type` VARCHAR(100),
        `quantity` VARCHAR(50),
        `quantity_int` INT,
        `specs` VARCHAR(200),
        `spec_value` DECIMAL(18,2),
        `spec_unit` VARCHAR(20),
        `trade_type` VARCHAR(50),
        `shipowner` VARCHAR(200),
        `shipyard` VARCHAR(200),
        `buyer` VARCHAR(200),
        `seller` VARCHAR(200),
        `charterer` VARCHAR(200),
        `owner` VARCHAR(200),
        `delivery` VARCHAR(200),
        `power` VARCHAR(100),
        `charter_type` VARCHAR(50),
        `original_price` VARCHAR(200),
        `currency` VARCHAR(20),
        `cny_min` DECIMAL(18,4),
        `cny_max` DECIMAL(18,4),
        `cny_display` VARCHAR(200),
        `is_rate` TINYINT(1) DEFAULT 0,
        `confidence_score` INT,
        `raw_transaction` JSON,
        `tx_hash` VARCHAR(64) NOT NULL,
        `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY `uk_tx_hash` (`tx_hash`),
        KEY `idx_news_id` (`news_id`),
        KEY `idx_date` (`news_date`),
        KEY `idx_ship_type` (`ship_type`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    cursor = conn.cursor()
    try:
        for category, table_name in _MARKET_TABLES.items():
            cursor.execute(base_sql.format(table_name=table_name))
        conn.commit()
    finally:
        cursor.close()
    # 为旧表补齐新字段
    _ensure_spec_columns(conn)


def _ensure_spec_columns(conn) -> None:
    """为已存在的三张表补齐 spec_value / spec_unit 字段。"""
    cursor = conn.cursor()
    try:
        for category, table_name in _MARKET_TABLES.items():
            for col_name, col_def in [
                ("spec_value", "DECIMAL(18,2)"),
                ("spec_unit", "VARCHAR(20)"),
            ]:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = %s
                      AND COLUMN_NAME = %s
                    """,
                    (table_name, col_name),
                )
                if cursor.fetchone()[0] == 0:
                    cursor.execute(
                        f"ALTER TABLE `{table_name}` ADD COLUMN `{col_name}` {col_def}"
                    )
        conn.commit()
    finally:
        cursor.close()


def _deduplicate_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按交易主体去重：同一市场类别下船型与交易双方均相同视为重复，保留第一条。"""
    seen = set()
    result = []
    for r in records:
        cat = r.get("category")
        ship_type = r.get("ship_type") or ""
        if cat == "新造船订单":
            party_a = r.get("shipowner") or ""
            party_b = r.get("shipyard") or ""
        elif cat == "买卖船":
            party_a = r.get("buyer") or ""
            party_b = r.get("seller") or ""
        elif cat == "租船":
            party_a = r.get("charterer") or ""
            party_b = r.get("owner") or ""
        else:
            party_a = party_b = ""
        # 至少有一方主体信息时才参与去重，避免无主体记录被过度合并
        if party_a or party_b:
            key = (cat, ship_type, party_a, party_b)
            if key in seen:
                continue
            seen.add(key)
        result.append(r)
    return result


def _fetch_records_from_table(
    conn,
    category: str,
    start_date: Optional[str],
    end_date: Optional[str],
    keyword: Optional[str],
    ship_type: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """从对应类别的市场分析表读取已归一化的交易记录。"""
    table_name = _get_market_table(category)
    conditions: List[str] = []
    params: List[Any] = []

    if start_date:
        conditions.append("news_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("news_date <= %s")
        params.append(end_date)
    if keyword:
        conditions.append("(news_title LIKE %s OR news_summary LIKE %s)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if ship_type:
        conditions.append("ship_type LIKE %s")
        params.append(f"%{ship_type}%")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT
            `id`, `news_id`, `news_title`, `news_link`, `news_date`, `source_platform`,
            `category_key`, `date`, `ship_type`, `quantity`, `specs`, `spec_value`, `spec_unit`,
            `trade_type`, `shipowner`, `shipyard`, `buyer`, `seller`, `charterer`, `owner`,
            `delivery`, `power`, `charter_type`, `original_price`, `currency`,
            `cny_min`, `cny_max`, `cny_display`, `is_rate`, `confidence_score`
        FROM `{table_name}`
        {where_clause}
        ORDER BY `news_date` DESC, `news_id` DESC
        LIMIT %s
    """
    params.append(limit)

    cursor = conn.cursor(dictionary=True)
    rows: List[Dict[str, Any]] = []
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    finally:
        cursor.close()

    records: List[Dict[str, Any]] = []
    for row in rows:
        record: Dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, Decimal):
                v = float(v)
            elif isinstance(v, (date, datetime)):
                v = v.strftime("%Y-%m-%d")
            record[k] = v
        record["is_rate"] = bool(record.get("is_rate"))
        # 新表按表名区分类别，查询结果补充 category 字段以保持上下游兼容
        record["category"] = category
        records.append(record)
    return records


def fetch_market_records(
    category: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    keyword: Optional[str] = None,
    ship_type: Optional[str] = None,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    """从对应类别的市场分析表读取已归一化、去重、可复用的交易记录。"""
    if category not in _CATEGORY_MAP.values():
        raise ValueError(f"不支持的市场类别: {category}")

    settings = get_settings()
    db_config = settings.NEWS_DB_CONFIG
    conn = mysql.connector.connect(**db_config)

    try:
        _ensure_market_analysis_table(conn)
        records = _fetch_records_from_table(
            conn, category, start_date, end_date, keyword, ship_type, limit
        )
    finally:
        conn.close()

    # 按交易主体去重
    records = _deduplicate_records(records)
    # 统计分析去除明显的价格噪音
    records = _filter_price_outliers(records)

    return records


def update_market_record(
    record_id: int,
    category_key: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """更新指定类别市场分析表中的单条交易记录，必要时重新换算人民币价格。"""
    category = _CATEGORY_MAP.get(category_key, category_key)
    if category not in _MARKET_TABLES:
        raise ValueError(f"不支持的市场类别: {category_key}")
    table_name = _get_market_table(category)

    settings = get_settings()
    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    try:
        _ensure_market_analysis_table(conn)

        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"SELECT * FROM `{table_name}` WHERE id = %s",
                (record_id,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()

        if not row:
            raise ValueError(f"未找到记录: {record_id}")

        # 当前记录作为更新基础
        record: Dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, Decimal):
                v = float(v)
            elif isinstance(v, (date, datetime)):
                v = v.strftime("%Y-%m-%d")
            record[k] = v
        record["is_rate"] = bool(record.get("is_rate"))
        record["category"] = category

        # 允许直接更新的字段
        editable_fields = {
            "date", "ship_type", "specs", "quantity", "original_price", "cny_min", "cny_max", "trade_type",
            "delivery", "power", "charter_type", "shipowner", "shipyard",
            "buyer", "seller", "charterer", "owner",
        }
        for field, value in updates.items():
            if field in editable_fields:
                record[field] = value

        # 若规格或船型发生变化，重新归一化规格字段
        spec_changed = any(f in updates for f in ("specs", "ship_type"))
        if spec_changed:
            spec_info = _parse_specs(record.get("specs"), record.get("ship_type"))
            record.update(spec_info)

        # 若价格、数量或船型发生变化，重新换算人民币价格
        price_changed = any(
            f in updates for f in ("original_price", "quantity", "ship_type")
        )
        if price_changed:
            quantity_int = _parse_quantity(record.get("quantity"))
            price_info = _normalize_price(record.get("original_price"), category, quantity_int)
            record.update(price_info)
        elif any(f in updates for f in ("cny_min", "cny_max")):
            # 用户直接修改归一化价格时，仅更新显示字段，不重新换算
            record["cny_display"] = format_cny_range(record.get("cny_min"), record.get("cny_max"))

        # 构造更新 SQL（排除 id、news_id、created_at、updated_at、tx_hash、raw_transaction）
        columns_to_update = [
            "date", "ship_type", "quantity", "quantity_int", "specs", "spec_value", "spec_unit", "trade_type",
            "shipowner", "shipyard", "buyer", "seller", "charterer", "owner",
            "delivery", "power", "charter_type", "original_price", "currency",
            "cny_min", "cny_max", "cny_display", "is_rate",
        ]
        set_clause = ", ".join(f"`{c}` = %s" for c in columns_to_update)
        params = [record.get(c) for c in columns_to_update]
        params.append(record_id)

        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE `{table_name}` SET {set_clause} WHERE id = %s",
                params,
            )
            conn.commit()
        finally:
            cursor.close()

        # 重新读取返回（带上最新 updated_at）
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT
                    `id`, `news_id`, `news_title`, `news_link`, `news_date`, `source_platform`,
                    `category_key`, `date`, `ship_type`, `quantity`, `specs`, `spec_value`, `spec_unit`,
                    `trade_type`, `shipowner`, `shipyard`, `buyer`, `seller`, `charterer`, `owner`,
                    `delivery`, `power`, `charter_type`, `original_price`, `currency`,
                    `cny_min`, `cny_max`, `cny_display`, `is_rate`, `confidence_score`
                FROM `{table_name}` WHERE id = %s
                """,
                (record_id,),
            )
            updated_row = cursor.fetchone()
        finally:
            cursor.close()

        result: Dict[str, Any] = {}
        for k, v in updated_row.items():
            if isinstance(v, Decimal):
                v = float(v)
            elif isinstance(v, (date, datetime)):
                v = v.strftime("%Y-%m-%d")
            result[k] = v
        result["is_rate"] = bool(result.get("is_rate"))
        result["category"] = category
        return result
    finally:
        conn.close()


def get_last_sync_time(category: str) -> Optional[str]:
    """获取指定类别最近一次同步/更新的时间（ISO 格式字符串）。"""
    if category not in _CATEGORY_MAP.values():
        raise ValueError(f"不支持的市场类别: {category}")

    table_name = _get_market_table(category)
    settings = get_settings()
    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    try:
        _ensure_market_analysis_table(conn)
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT MAX(updated_at) FROM `{table_name}`",
            )
            row = cursor.fetchone()
            max_updated = row[0] if row else None
            if max_updated:
                return max_updated.isoformat() if hasattr(max_updated, "isoformat") else str(max_updated)
            return None
        finally:
            cursor.close()
    finally:
        conn.close()


def aggregate_by_date(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按日期聚合，计算均价、交易笔数。"""
    from collections import defaultdict

    groups = defaultdict(list)
    for r in records:
        d = r.get("date") or r.get("news_date")
        if d:
            groups[str(d)[:10]].append(r)

    result = []
    for d in sorted(groups.keys()):
        txs = groups[d]
        prices = []
        for t in txs:
            if t.get("cny_min") is not None:
                prices.append(t["cny_min"])
            if t.get("cny_max") is not None and t["cny_max"] != t["cny_min"]:
                prices.append(t["cny_max"])
        if prices:
            avg = sum(prices) / len(prices)
            result.append({
                "date": d,
                "count": len(txs),
                "avg_price": round(avg, 2),
                "min_price": round(min(prices), 2),
                "max_price": round(max(prices), 2),
            })
    return result


def aggregate_by_ship_type(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按船型聚合。"""
    from collections import defaultdict

    groups = defaultdict(list)
    for r in records:
        st = r.get("ship_type") or "未知船型"
        groups[st].append(r)

    result = []
    for st, txs in groups.items():
        prices = []
        for t in txs:
            if t.get("cny_min") is not None:
                prices.append(t["cny_min"])
            if t.get("cny_max") is not None and t["cny_max"] != t["cny_min"]:
                prices.append(t["cny_max"])
        result.append({
            "ship_type": st,
            "count": len(txs),
            "avg_price": round(sum(prices) / len(prices), 2) if prices else None,
            "min_price": round(min(prices), 2) if prices else None,
            "max_price": round(max(prices), 2) if prices else None,
        })
    # 按交易数量降序
    result.sort(key=lambda x: x["count"], reverse=True)
    return result


def compute_ma(prices: List[Optional[float]], window: int = 5) -> List[Optional[float]]:
    """计算简单移动平均。"""
    if not prices:
        return []
    result = []
    for i in range(len(prices)):
        start = max(0, i - window + 1)
        window_values = [p for p in prices[start : i + 1] if p is not None]
        if window_values:
            result.append(round(sum(window_values) / len(window_values), 2))
        else:
            result.append(None)
    return result

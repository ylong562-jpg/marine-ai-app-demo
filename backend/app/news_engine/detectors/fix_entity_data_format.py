"""
entity/data 格式修复模块

功能：
1. 从数据库读取 13 个分类的新闻
2. 检测 entity 和 data 字段是否为合法 JSON 且结构正确
3. 格式错误的，使用 AI 重新生成 entity/data
4. 格式正确的直接跳过
"""

import sys
import os

# 确保当前目录在 Python 路径中
if getattr(sys, 'frozen', False):
    current_dir = os.path.dirname(sys.executable)
    internal_dir = os.path.join(current_dir, '_internal')
    if os.path.exists(internal_dir):
        sys.path.insert(0, internal_dir)
    sys.path.insert(0, current_dir)
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

import mysql.connector

from ai_engine import BailianWebAnalyzer
from prompt import (
    newbuilding_orders_prompt_entity,
    data_prompt_newbuilding_orders,
    repair_conversion_prompt_entity,
    data_prompt_repair_conversion,
    ship_building_prompt_entity,
    data_prompt_ship_building,
    ship_sale_prompt_entity,
    data_prompt_ship_sale,
    charter_sale_prompt_entity,
    data_prompt_charter_sale,
)
from ai_news_report_creating import (
    parse_ship_order_report,
    parse_ship_sale_report,
    parse_charter_report,
    parse_ship_building_report,
)
from entity_normalizer import normalize_shipyard_entity


# 数据库配置
from db_config import DB_CONFIG_MYSQL as DEFAULT_DB_CONFIG

# 需要处理的主维度（7 个核心主维度）
TARGET_CATEGORIES = [
    "新造船订单", "买卖船", "租船", "船舶建造",
    "维修改装", "公司金融", "行业新闻"
]

# 旧 13 类 -> 新主维度映射（用于兼容历史数据）
LEGACY_CATEGORY_MAP = {
    "新造船订单": "新造船订单",
    "买卖船": "买卖船",
    "租船": "租船",
    "租船购船": "行业新闻",  # 旧分类无法区分，保守兜底
    "船只交付": "船舶建造",
    "船舶建造": "船舶建造",
    "生产节点": "船舶建造",
    "国际局势": "行业新闻",
    "维修改装": "维修改装",
    "公司金融": "公司金融",
    "行业综合动态": "行业新闻",
    # 原主维度降级为次标签，历史数据归入兜底
    "企业合作与投资": "行业新闻",
    "地缘政治与冲突": "行业新闻",
    "运河航道运营": "行业新闻",
    "船舶规范与认证": "行业新闻",
    "航运市场分析": "行业新闻",
    "绿色航运": "行业新闻",
    "船厂生产节点": "船舶建造",
    "行业事故": "行业新闻",
    "人事调动": "行业新闻",
    "政策法规": "行业新闻",
    "企业日常动态": "行业新闻",
    "技术突破": "行业新闻",
    "行业会议/活动": "行业新闻",
}


def _extract_groups(field_text):
    """从新的数组格式中提取分组列表。兼容旧格式。"""
    if not field_text:
        return []
    data = parse_json_safe(field_text)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    # 旧格式：单个字典
    return [data]


def _get_main_tag_from_group(group, default=""):
    """获取分组的主维度标签。"""
    if isinstance(group, dict) and group.get("main_tag"):
        return group["main_tag"]
    return default


def _get_inner_field(group, field_name):
    """获取分组内部的 entity 或 data 字段值。"""
    if isinstance(group, dict) and field_name in group:
        return group[field_name]
    return group


def connect_db(db_config=None):
    """连接数据库"""
    config = db_config or DEFAULT_DB_CONFIG
    return mysql.connector.connect(**config)


def build_date_where_clause(days=None, start_date=None, end_date=None):
    """构建日期筛选条件"""
    conditions = []
    params = []
    if days:
        start = (datetime.now() - timedelta(days=int(days))).strftime('%Y-%m-%d')
        conditions.append("date >= %s")
        params.append(start)
    if start_date:
        conditions.append("date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("date <= %s")
        params.append(end_date)
    return (" AND " + " AND ".join(conditions)) if conditions else "", params


def fetch_target_news(conn, days=None, start_date=None, end_date=None, limit=None):
    """从数据库获取目标新闻（不再按旧 category 过滤，按日期范围获取）"""
    cursor = conn.cursor(dictionary=True)

    date_where, date_params = build_date_where_clause(days, start_date, end_date)

    query = f"""
    SELECT id, title, link, date,
           entity, data, summary, news_report, tags, source_platform
    FROM ship_news
    {('WHERE ' + date_where[5:]) if date_where else ''}
    ORDER BY date DESC, id DESC
    """

    cursor.execute(query, date_params)
    rows = cursor.fetchall()
    cursor.close()

    if limit:
        rows = rows[:limit]

    return rows


def is_valid_json(text):
    """检查文本是否为合法 JSON"""
    if not text or not isinstance(text, str):
        return False
    try:
        json.loads(text.strip())
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def parse_json_safe(text):
    """安全解析 JSON，失败返回 None"""
    if not text or not isinstance(text, str):
        return None
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        return None


def _check_entity_fields(item, category):
    """检查单个 entity 字典是否包含所需字段。"""
    keys = set(item.keys()) if isinstance(item, dict) else set()
    required_map = {
        "新造船订单": {"shipowner", "shipowners", "shipyard", "shipyards"},
        "买卖船": {"buyer", "buyers", "seller", "sellers"},
        "租船": {"charterer", "charterers", "owner", "owners"},
        "船舶建造": {"shipowner", "shipowners", "shipyard", "shipyards"},
        "公司金融": {"company", "companies", "entities", "entity"},
        "行业新闻": {"country", "countries", "organization", "organizations", "entities", "entity"},
        "维修改装": {"shipowner", "shipowners", "shipyard", "shipyards"},
    }
    expected = required_map.get(category, set())
    if not expected:
        return True
    return bool(keys & expected)


_ENTITY_PARTY_FIELDS = {
    "新造船订单": {"shipowner", "shipowners", "shipyard", "shipyards"},
    "买卖船": {"buyer", "buyers", "seller", "sellers"},
    "租船": {"charterer", "charterers", "owner", "owners"},
    "船舶建造": {"shipowner", "shipowners", "shipyard", "shipyards"},
    "维修改装": {"shipowner", "shipowners", "facility", "facilities"},
    "公司金融": {"parties", "company", "companies", "entities", "entity"},
    "行业新闻": {"subject", "entities", "entity", "organization", "organizations", "country", "countries"},
}


def _data_has_party_fields(category, data_text):
    """检查 data（含 transactions）中是否包含对应类别的主体字段。"""
    if not data_text:
        return False
    data = parse_json_safe(data_text)
    if data is None:
        return False

    party_fields = _ENTITY_PARTY_FIELDS.get(category, set())
    if not party_fields:
        return False

    def _is_valid_party_value(value):
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip() not in ("", "None", "null", "NULL", "nan", "NaN")
        if isinstance(value, (list, dict, set)):
            return len(value) > 0
        return True

    def _check_obj(obj):
        if not isinstance(obj, dict):
            return False
        return any(_is_valid_party_value(obj.get(k)) for k in party_fields)

    def _walk(obj):
        if isinstance(obj, dict):
            if "transactions" in obj and isinstance(obj["transactions"], list):
                if any(_check_obj(t) for t in obj["transactions"] if isinstance(t, dict)):
                    return True
            if _check_obj(obj):
                return True
            return any(_walk(v) for v in obj.values())
        elif isinstance(obj, list):
            return any(_walk(item) for item in obj)
        return False

    return _walk(data)


def validate_entity(category, entity, data=None):
    """验证 entity 字段格式是否正确。支持新的数组格式、单个字典或字典数组。
    新格式下 entity 可为空，只要 data.transactions 中包含对应主体字段即可。
    """
    effective_category = LEGACY_CATEGORY_MAP.get(category, category)

    # 新格式：entity 为空/null/空数组，但 data 中有主体字段，视为通过
    if not entity or str(entity).strip() in ("", "null", "None", "[]"):
        if data and _data_has_party_fields(effective_category, data):
            return True, "entity 为空但 data 中已包含主体字段"
        if not is_valid_json(entity) if entity else False:
            return False, "不是合法 JSON"
        return False, "entity 为空且 data 中未找到主体字段"

    if not is_valid_json(entity):
        return False, "不是合法 JSON"

    parsed = parse_json_safe(entity)
    groups = _extract_groups(entity)

    if groups:
        for idx, group in enumerate(groups):
            if not isinstance(group, dict):
                return False, f"entity 数组第 {idx + 1} 项不是字典"
            main_tag = _get_main_tag_from_group(group, effective_category)
            inner = _get_inner_field(group, "entity")
            if isinstance(inner, list):
                for j, item in enumerate(inner):
                    if not isinstance(item, dict):
                        return False, f"entity 第 {idx + 1} 组第 {j + 1} 项不是字典"
                    if not _check_entity_fields(item, main_tag or effective_category):
                        return False, f"entity 第 {idx + 1} 组第 {j + 1} 项缺少必要字段"
            elif isinstance(inner, dict):
                if not _check_entity_fields(inner, main_tag or effective_category):
                    return False, f"entity 第 {idx + 1} 组缺少必要字段"
            elif not inner:
                # 分组中 entity 为空，检查对应 data 是否有主体
                if data and _data_has_party_fields(main_tag or effective_category, data):
                    continue
                return False, f"entity 第 {idx + 1} 组内容为空"
        return True, "OK"

    if not isinstance(parsed, dict):
        return False, "JSON 不是字典或字典数组"

    if not _check_entity_fields(parsed, effective_category):
        return False, "entity 缺少必要字段"

    return True, "OK"


QUANTITY_CATEGORIES = {
    "新造船订单", "买卖船", "租船", "船舶建造", "维修改装"
}


def _is_valid_quantity(value):
    """校验 quantity/all_quantity 字段格式。

    允许：
    - 整数/浮点数
    - 纯数字字符串（可带 '艘'/'条' 单位，自动去除）
    - 确认+备选格式，如 '2+2'、'2+2（备选）' 或 '2+2(备选)'
      '+' 前为即时生效数量，'+' 后为备选/选择权数量。
    """
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    s = value.strip().replace(" ", "").replace("艘", "").replace("条", "")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return True
    # 允许 X+Y、X+Y（备选）、X+Y(备选)
    if re.fullmatch(r"\d+\+\d+(?:（备选）|\(备选\))?", s):
        return True
    return False


def _check_data_transaction(transaction, category):
    """检查单个交易/交付字典是否包含所需字段（含主体字段）。"""
    expected_map = {
        "新造船订单": {"date", "shipowner", "shipyard", "ship_type", "quantity", "specs", "price", "delivery", "power"},
        "买卖船": {"date", "buyer", "seller", "ship_type", "quantity", "specs", "money", "delivery"},
        "租船": {"date", "charterer", "owner", "ship_type", "quantity", "specs", "money", "delivery", "charter_type"},
        "船舶建造": {"date", "event_type", "shipowner", "shipyard", "ship_type", "specs", "vessel_name"},
        "公司金融": {"event_type", "amount", "parties", "period", "metrics"},
        "行业新闻": {"event_type", "date", "subject", "key_info", "location", "affected_routes", "impact", "metrics"},
        "维修改装": {"date", "shipowner", "ship_type", "specs", "modify_type", "facility", "vessel_name"},
    }
    expected = expected_map.get(category, set())
    if not expected:
        return True
    return any(k in transaction for k in expected)


def _count_entity_transactions(entity_text):
    """计算 entity 中包含的交易/交付数量。"""
    data = parse_json_safe(entity_text)
    if data is None:
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return 1
    return None


def _count_data_transactions(data_text):
    """计算 data 中包含的交易/交付数量。"""
    data = parse_json_safe(data_text)
    if data is None:
        return None
    if isinstance(data, dict):
        if "transactions" in data:
            transactions = data.get("transactions")
            if isinstance(transactions, list):
                return len(transactions)
            return None
        return 1
    return None


def validate_entity_data_consistency(entity_text, data_text, category=None):
    """验证 entity 与 data 的交易数量是否严格一致。
    新格式下 entity 可为空，若 entity 为空或 data 中已包含主体字段，不再强制要求数量一致。
    """
    entity_count = _count_entity_transactions(entity_text)
    data_count = _count_data_transactions(data_text)

    # 新格式：entity 为空，直接通过
    if entity_count is None or entity_count == 0:
        return True, "OK"
    if data_count is None:
        return True, "OK"

    # 旧格式：要求数量一致
    if entity_count != data_count:
        return False, f"entity 交易数量 ({entity_count}) 与 data 交易数量 ({data_count}) 不一致"
    return True, "OK"


def validate_data(category, data_text):
    """验证 data 字段格式是否正确。支持新的数组格式、单笔（平铺字典）或多笔（transactions数组 + all_*）。"""
    if not is_valid_json(data_text):
        return False, "不是合法 JSON"

    data = parse_json_safe(data_text)
    groups = _extract_groups(data_text)
    effective_category = LEGACY_CATEGORY_MAP.get(category, category)

    if groups:
        for idx, group in enumerate(groups):
            if not isinstance(group, dict):
                return False, f"data 数组第 {idx + 1} 项不是字典"
            main_tag = _get_main_tag_from_group(group, effective_category)
            inner = _get_inner_field(group, "data")
            if not isinstance(inner, dict):
                return False, f"data 第 {idx + 1} 组不是字典"
            ok, reason = _validate_single_data(main_tag or effective_category, inner)
            if not ok:
                return False, f"data 第 {idx + 1} 组: {reason}"
        return True, "OK"

    if not isinstance(data, dict):
        return False, "JSON 不是字典"

    return _validate_single_data(effective_category, data)


def _validate_single_data(category, data):
    """验证单个 data 字典。"""
    # 多笔交易新格式：包含 transactions 数组
    if "transactions" in data:
        transactions = data.get("transactions")
        if not isinstance(transactions, list):
            return False, "transactions 不是数组"
        if not transactions:
            return False, "transactions 数组为空"
        for idx, transaction in enumerate(transactions):
            if not isinstance(transaction, dict):
                return False, f"transactions 第 {idx + 1} 项不是字典"
            if not _check_data_transaction(transaction, category):
                return False, f"transactions 第 {idx + 1} 项缺少任何预期字段"
            if category in QUANTITY_CATEGORIES:
                quantity = transaction.get("quantity")
                if quantity is not None and not _is_valid_quantity(quantity):
                    return False, f"transactions 第 {idx + 1} 项 quantity 格式错误: {quantity}"
        # 检查外层 all_* 字段类型
        if "all_quantity" in data and data.get("all_quantity") is not None and not isinstance(data.get("all_quantity"), (str, int, float)):
            return False, "all_quantity 类型错误"
        if category in QUANTITY_CATEGORIES:
            all_quantity = data.get("all_quantity")
            if all_quantity is not None and not _is_valid_quantity(all_quantity):
                return False, f"all_quantity 格式错误: {all_quantity}"
        if "all_price" in data and data.get("all_price") is not None and not isinstance(data.get("all_price"), (str, int, float)):
            return False, "all_price 类型错误"
        return True, "OK"

    # 单笔交易：平铺字典
    if not _check_data_transaction(data, category):
        return False, "data 缺少任何预期字段"

    if category in QUANTITY_CATEGORIES:
        quantity = data.get("quantity")
        if quantity is not None and not _is_valid_quantity(quantity):
            return False, f"quantity 格式错误: {quantity}"

    return True, "OK"


def get_prompts_for_field(category, field):
    """根据分类/字段获取对应的 prompt（统一使用 prompt.py 的 category_prompt_groups）"""
    from prompt import get_prompts_by_category
    prompts = get_prompts_by_category(category)
    if not prompts or len(prompts) < 2:
        return None
    if field == "entity":
        return prompts[0]
    if field == "data":
        return prompts[1]
    return None


def build_regeneration_context(news):
    """构建用于 AI 重新生成的上下文"""
    parts = []
    parts.append(f"标题：{news.get('title', '')}")
    if news.get('summary'):
        parts.append(f"摘要：{news['summary']}")
    if news.get('news_report'):
        parts.append(f"现有新闻报告：{news['news_report']}")
    if news.get('entity'):
        parts.append(f"现有实体信息（格式可能错误）：{news['entity']}")
    if news.get('data'):
        parts.append(f"现有数据信息（格式可能错误）：{news['data']}")
    if news.get('link'):
        parts.append(f"来源链接：{news['link']}")
    return "\n".join(parts)


async def regenerate_field(analyzer, news, field, semaphore):
    """使用 AI 重新生成单个字段（entity 或 data）"""
    async with semaphore:
        category = news.get('category', '')
        prompt = get_prompts_for_field(category, field)

        if not prompt:
            print(f"  ⚠️ 未找到 {category}/{field} 对应的 prompt")
            return None

        full_prompt = f"【重要约束】回答中严格去除新闻发布方/媒体平台名称。\\n\\n{prompt}\\n\\n【再次强调】你必须且只能返回合法JSON对象/数组，不要输出任何解释、总结、前言或后缀文字。"
        context = build_regeneration_context(news)

        try:
            result = await analyzer.analyze(
                content=context,
                prompt=full_prompt,
                max_tokens=400,
                model="qwen-plus",
            )

            if not result["success"]:
                print(f"  ❌ AI 生成失败: {result.get('error', '未知错误')}")
                return None

            analysis = result["analysis"].strip()

            # 尝试提取 JSON
            try:
                # 先直接解析
                parsed = json.loads(analysis)
                # 对船厂名称做标准化
                parsed = normalize_shipyard_entity(parsed)
                return json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass

            # 尝试从文本中提取 {...} 或 [...]
            for start_char, end_char in [("{", "}"), ("[", "]")]:
                start = analysis.find(start_char)
                if start != -1:
                    depth = 0
                    for i in range(start, len(analysis)):
                        if analysis[i] == start_char:
                            depth += 1
                        elif analysis[i] == end_char:
                            depth -= 1
                            if depth == 0:
                                candidate = analysis[start:i + 1]
                                try:
                                    parsed = json.loads(candidate)
                                    parsed = normalize_shipyard_entity(parsed)
                                    return json.dumps(parsed, ensure_ascii=False)
                                except (json.JSONDecodeError, TypeError):
                                    break

            print(f"  ⚠️ AI 返回内容无法解析为 JSON: {analysis[:80]}")
            return None

        except Exception as e:
            print(f"  ❌ 重新生成 {field} 异常: {str(e)[:80]}")
            return None


def regenerate_newbuilding_news_report(entity, data):
    """重新生成新造船订单的新闻报告"""
    try:
        return parse_ship_order_report(entity, data)
    except Exception as e:
        print(f"  ⚠️ 重新生成 news_report 失败: {str(e)[:80]}")
        return None


def regenerate_ship_sale_news_report(entity, data):
    """重新生成买卖船的新闻报告"""
    try:
        return parse_ship_sale_report(entity, data)
    except Exception as e:
        print(f"  ⚠️ 重新生成 news_report 失败: {str(e)[:80]}")
        return None


def regenerate_charter_news_report(entity, data):
    """重新生成租船的新闻报告"""
    try:
        return parse_charter_report(entity, data)
    except Exception as e:
        print(f"  ⚠️ 重新生成 news_report 失败: {str(e)[:80]}")
        return None


def regenerate_ship_building_news_report(entity, data):
    """重新生成船舶建造的新闻报告"""
    try:
        return parse_ship_building_report(entity, data)
    except Exception as e:
        print(f"  ⚠️ 重新生成 news_report 失败: {str(e)[:80]}")
        return None


async def process_news_item(analyzer, news, semaphore):
    """处理单条新闻：检测并修复 entity/data 格式（兼容新旧格式）"""
    category = news.get('category', '')
    effective_category = LEGACY_CATEGORY_MAP.get(category, category)
    
    news_id = news.get('id')
    title = news.get('title', '')

    print(f"\n🔍 处理 [{effective_category or '无旧分类'}] {title[:50]}...")

    entity_ok, entity_reason = validate_entity(effective_category, news.get('entity', ''), data=news.get('data', ''))
    data_ok, data_reason = validate_data(effective_category, news.get('data', ''))

    updates = {}

    # 单独格式正确后，进一步校验 entity 与 data 交易数量是否一致
    if entity_ok and data_ok:
        consistent, consistency_reason = validate_entity_data_consistency(
            news.get('entity', ''), news.get('data', ''), category=effective_category
        )
        if not consistent:
            print(f"  ⚠️ entity/data 数量不一致: {consistency_reason}")
            entity_ok = False
            data_ok = False
            entity_reason = consistency_reason
            data_reason = consistency_reason
        else:
            print(f"  ✅ entity 和 data 格式均正确，跳过")
            return None

    # 兼容：旧数据没有 tags 时，使用 effective_category 作为单一主维度
    tags = news.get('tags', '')
    main_tags = []
    if tags:
        try:
            tags_parsed = json.loads(tags)
            if isinstance(tags_parsed, dict):
                main_tags = [t for t in tags_parsed.get('main_tags', []) if t in TARGET_CATEGORIES]
        except Exception:
            pass
    if not main_tags and effective_category in TARGET_CATEGORIES:
        main_tags = [effective_category]

    if not main_tags:
        main_tags = ["行业新闻"]

    if not entity_ok:
        print(f"  ⚠️ entity 格式错误: {entity_reason}")
        # 简化处理：按第一个主维度重新生成整个 entity 字段
        temp_news = news.copy()
        temp_news['category'] = main_tags[0]
        new_entity = await regenerate_field(analyzer, temp_news, "entity", semaphore)
        if new_entity:
            ok, reason = validate_entity(effective_category, new_entity)
            if ok:
                updates['entity'] = new_entity
                print(f"  ✅ entity 重新生成并验证通过")
            else:
                print(f"  ⚠️ 重新生成的 entity 仍不通过: {reason}")

    if not data_ok:
        print(f"  ⚠️ data 格式错误: {data_reason}")
        temp_news = news.copy()
        if 'entity' in updates:
            temp_news['entity'] = updates['entity']
        temp_news['category'] = main_tags[0]
        new_data = await regenerate_field(analyzer, temp_news, "data", semaphore)
        if new_data:
            ok, reason = validate_data(effective_category, new_data)
            if ok:
                updates['data'] = new_data
                print(f"  ✅ data 重新生成并验证通过")
            else:
                print(f"  ⚠️ 重新生成的 data 仍不通过: {reason}")

    # 根据主维度同步更新 news_report
    if main_tags[0] == "新造船订单" and updates:
        final_entity = updates.get('entity', news.get('entity', ''))
        final_data = updates.get('data', news.get('data', ''))
        new_report = regenerate_newbuilding_news_report(final_entity, final_data)
        if new_report:
            updates['news_report'] = new_report
            print(f"  ✅ news_report 已同步更新")

    if main_tags[0] == "买卖船" and updates:
        final_entity = updates.get('entity', news.get('entity', ''))
        final_data = updates.get('data', news.get('data', ''))
        new_report = regenerate_ship_sale_news_report(final_entity, final_data)
        if new_report:
            updates['news_report'] = new_report
            print(f"  ✅ news_report 已同步更新")

    if main_tags[0] == "租船" and updates:
        final_entity = updates.get('entity', news.get('entity', ''))
        final_data = updates.get('data', news.get('data', ''))
        new_report = regenerate_charter_news_report(final_entity, final_data)
        if new_report:
            updates['news_report'] = new_report
            print(f"  ✅ news_report 已同步更新")

    if main_tags[0] == "船舶建造" and updates:
        final_entity = updates.get('entity', news.get('entity', ''))
        final_data = updates.get('data', news.get('data', ''))
        new_report = regenerate_ship_building_news_report(final_entity, final_data)
        if new_report:
            updates['news_report'] = new_report
            print(f"  ✅ news_report 已同步更新")

    # 最终一致性校验：数量不一致则放弃本次更新，避免写入错误数据
    if updates and ('entity' in updates or 'data' in updates):
        final_entity = updates.get('entity', news.get('entity', ''))
        final_data = updates.get('data', news.get('data', ''))
        consistent, consistency_reason = validate_entity_data_consistency(
            final_entity, final_data, category=effective_category
        )
        if not consistent:
            print(f"  ❌ 重新生成后 entity/data 仍不一致: {consistency_reason}，放弃更新")
            return {}

    return updates


def update_news_in_db(conn, news_id, updates):
    """更新数据库中的新闻记录"""
    if not updates:
        return False

    fields = []
    params = []
    for key, value in updates.items():
        fields.append(f"{key} = %s")
        params.append(value)
    params.append(news_id)

    query = f"UPDATE ship_news SET {', '.join(fields)} WHERE id = %s"

    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        cursor.close()
        print(f"  ❌ 数据库更新失败 (id={news_id}): {str(e)[:80]}")
        return False


async def run_fix_entity_data_format(days=None, start_date=None, end_date=None, limit=None, concurrent=3):
    """主运行函数"""
    print("\n" + "=" * 70)
    print("🔄 entity/data 格式修复")
    print("=" * 70)
    print(f"目标分类: {', '.join(TARGET_CATEGORIES)}")
    print(f"日期范围: {days or '未限制'}天" if days else f"日期范围: {start_date or '不限'} 至 {end_date or '不限'}")
    print(f"并发数: {concurrent}")
    print("=" * 70 + "\n")

    analyzer = BailianWebAnalyzer(region="beijing")
    semaphore = asyncio.Semaphore(concurrent)

    conn = connect_db()
    try:
        rows = fetch_target_news(conn, days, start_date, end_date, limit)
        print(f"📊 共获取 {len(rows)} 条目标分类新闻\n")

        skipped = 0
        fixed = 0
        failed = 0

        for i, news in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}]", end=" ")
            updates = await process_news_item(analyzer, news, semaphore)
            if updates is None:
                skipped += 1
            elif updates:
                success = update_news_in_db(conn, news['id'], updates)
                if success:
                    fixed += 1
                else:
                    failed += 1
            else:
                failed += 1

        print("\n" + "=" * 70)
        print("📊 处理结果")
        print("=" * 70)
        print(f"  跳过（格式正确）: {skipped} 条")
        print(f"  修复成功: {fixed} 条")
        print(f"  修复失败: {failed} 条")
        print("=" * 70)

    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(run_fix_entity_data_format(days=7))

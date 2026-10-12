# -*- coding: utf-8 -*-
import sys
import os

# 确保当前目录在 Python 路径中（支持 PyInstaller 打包环境）
if getattr(sys, 'frozen', False):
    current_dir = os.path.dirname(sys.executable)
    internal_dir = os.path.join(current_dir, '_internal')
    if os.path.exists(internal_dir):
        sys.path.insert(0, internal_dir)
    sys.path.insert(0, current_dir)
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mysql.connector
import re
import json
import asyncio
import sqlite3
import html
from datetime import datetime

from news_analysis.backend.common.config import is_vpn_needed
from ai_engine import BailianWebAnalyzer
from transaction_groups import (
    is_broad_subject_value,
    group_transactions_by_shared_subject,
)


# 主维度配置（7 个核心主维度），数字越小优先级越高
MAIN_DIMENSION_CONFIG = {
    "新造船订单": {"priority": 1, "color": "#8b7355"},
    "买卖船": {"priority": 2, "color": "#a0522d"},
    "租船": {"priority": 3, "color": "#4a6fa5"},
    "船舶建造": {"priority": 4, "color": "#5a7a6a"},
    "维修改装": {"priority": 5, "color": "#6b5b95"},
    "公司金融": {"priority": 6, "color": "#2980b9"},
    "行业新闻": {"priority": 7, "color": "#2c3e50"},
}

# 旧主维度/旧分类 -> 新主维度（用于历史数据兼容）
LEGACY_TO_MAIN_DIMENSION = {
    "新造船订单": "新造船订单",
    "租船购船": "行业新闻",  # 旧分类无法区分买卖船/租船，保守兜底
    "买卖船": "买卖船",
    "租船": "租船",
    "船只交付": "船舶建造",
    "船舶建造": "船舶建造",
    "生产节点": "船舶建造",
    "船厂生产节点": "船舶建造",
    "维修改装": "维修改装",
    "国际局势": "行业新闻",
    "地缘政治与冲突": "行业新闻",
    "公司金融": "公司金融",
    "行业综合动态": "行业新闻",
    "行业新闻": "行业新闻",
    "其它": "行业新闻",
}


def _first_main_tag_from_item(item):
    """从 tags.main_tags 中提取第一个主维度，用于替代已弃用的 category 字段。"""
    tags = item.get('tags', '')
    if not tags:
        return None
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            return None
    if isinstance(tags, dict):
        main_tags = tags.get('main_tags', [])
        if isinstance(main_tags, list) and main_tags:
            return main_tags[0]
    return None


# 英译辅助：优先使用词表中的官方英文名，然后通过 LLM 润色
ROUGH_TRANSLATION_PROMPT = """\
请将以下航运新闻的中英混排草稿润色为一段专业、流畅的英文报道。

要求：
1. 保留所有数字、日期、金额、交付时间、船舶数量等关键数据，金额数值不得改动。
2. 金额必须换算为英文习惯表述：中文数量级'万'/'亿'要改写为'm'/'bn'，如'3545万美元'译为'USD 35.45 million'，'1.4亿美元'译为'USD 140 million'，'2.2亿美元'译为'USD 220 million'，'2.4万美元/日'译为'US$24,000 per day'；币种必须保留，不得遗漏或改动。
3. 草稿中已经替换为公司/船厂/船东/船型的官方英文名称，这些英文名称必须保持原样，不得改写或回译为中文。
4. 将剩余中文内容翻译为自然、专业的英文，符合航运行业表达习惯。
5. 只输出润色后的英文报道正文，不要解释、前言、序号或字数统计。

草稿：
{draft}
"""

_ENTITY_DICT = None
_SHIP_DICT = None


def _pick_english_alias(canonical_name, aliases):
    """从别名列表中挑选最像官方英文名的别名（优先长且 ASCII 占比高的名称）。"""
    if not aliases:
        return None
    best_alias = None
    best_score = 0.0
    for alias in aliases:
        if not alias or alias == canonical_name or len(alias) < 5:
            continue
        ascii_chars = sum(1 for ch in alias if ord(ch) < 128)
        ratio = ascii_chars / len(alias) if alias else 0
        if ratio <= 0.5:
            continue
        # 综合得分：ASCII 占比 × 长度，优先更完整、更长的英文名
        score = ratio * len(alias)
        if score > best_score:
            best_score = score
            best_alias = alias
    return best_alias


def _load_translation_glossaries():
    """加载实体与船型的官方中英对照表（结果缓存）。"""
    global _ENTITY_DICT, _SHIP_DICT
    if _ENTITY_DICT is not None:
        return _ENTITY_DICT, _SHIP_DICT

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # 1) 实体词表：canonical_name -> 英文别名
    entity_dict = {}
    db_path = os.path.join(base_dir, 'data', 'entity_registry.db')
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT canonical_name, aliases FROM entities")
            for name, aliases_json in cur.fetchall():
                aliases = json.loads(aliases_json or '[]')
                en = _pick_english_alias(name, aliases)
                if en:
                    entity_dict[name] = en
            conn.close()
        except Exception as e:
            print(f"[WARN] 加载实体词表失败: {e}")

    # 2) 船型词表：canonical_name -> 英文别名
    ship_dict = {}
    tax_path = os.path.join(base_dir, 'data', 'ship_type_taxonomy.json')
    if os.path.exists(tax_path):
        try:
            with open(tax_path, 'r', encoding='utf-8') as f:
                taxonomy = json.load(f)
            for entry in taxonomy.get('entries', []):
                name = entry.get('canonical_name')
                aliases = entry.get('aliases', [])
                en = _pick_english_alias(name, aliases)
                if name and en:
                    ship_dict[name] = en
        except Exception as e:
            print(f"[WARN] 加载船型词表失败: {e}")

    _ENTITY_DICT = entity_dict
    _SHIP_DICT = ship_dict
    return entity_dict, ship_dict


def _apply_glossary_replacement(text, entity_dict, ship_dict):
    """基于词表，将中文实体/船型名称替换为官方英文名（长词优先）。"""
    if not text:
        return text
    combined = {**entity_dict, **ship_dict}
    # 按长度降序，避免短词误替换长词
    for key in sorted(combined.keys(), key=len, reverse=True):
        if key in text:
            text = text.replace(key, combined[key])
    return text


def _format_report_html(text):
    """将报告文本中的 HTML 特殊字符转义，并将换行符渲染为 HTML 换行；同时去除结尾的'以上'收尾词。"""
    if not text:
        return ''
    text = re.sub(r'[。，、；：！？\.（）()\s]*以上[。，、；：！？\.（）()\s]*$', '', text)
    return html.escape(text).replace('\n', '<br>')


_CATEGORY_ENTITY_FIELDS = {
    "新造船订单": ("shipowner", "shipyard"),
    "船舶建造": ("shipowner", "shipyard"),
    "买卖船": ("buyer", "seller"),
    "租船": ("charterer", "owner"),
    "维修改装": ("shipowner", "facility"),
    "公司金融": ("parties", "company"),
}


def _parse_json_loose(value):
    """尝试多次解析可能嵌套的 JSON 字符串。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    for _ in range(3):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, str):
                s = parsed
                continue
            return parsed
        except Exception:
            break
    return None


def _extract_entities_for_news(news):
    """从 news 的 data / entity 中提取该类目对应的主体字段。"""
    # 渲染阶段重组后的展示条目已预计算主体标签（仅来自被合并的交易），直接用
    precomputed = news.get('_entity_names')
    if precomputed is not None:
        return precomputed

    original = news.get('_original_item') or news
    main_tag = news.get('main_tag', '')
    fields = _CATEGORY_ENTITY_FIELDS.get(main_tag, ("shipowner", "shipyard"))
    names = []
    seen = set()

    def add_name(name):
        if not name:
            return
        if isinstance(name, (list, tuple)):
            for n in name:
                add_name(n)
            return
        s = str(name).strip()
        if not s or s.lower() in ("null", "none", "某船厂", "某船东"):
            return
        if s not in seen:
            seen.add(s)
            names.append(s)

    # 1) 优先从 data 读取
    data_raw = original.get('data')
    data_parsed = _parse_json_loose(data_raw)
    if not isinstance(data_parsed, list):
        data_parsed = [data_parsed] if data_parsed else []

    for entry in data_parsed:
        if not isinstance(entry, dict):
            continue
        # 若 data 是按主标签分组的格式，只取当前标签
        if entry.get('main_tag') and entry.get('main_tag') != main_tag:
            continue
        data_inner = entry.get('data')
        if isinstance(data_inner, str):
            data_inner = _parse_json_loose(data_inner)
        if not isinstance(data_inner, list):
            data_inner = [data_inner] if data_inner else []

        for it in data_inner:
            if not isinstance(it, dict):
                continue
            transactions = it.get('transactions')
            if isinstance(transactions, list):
                for tx in transactions:
                    if isinstance(tx, dict):
                        for field in fields:
                            add_name(tx.get(field))
            # 对非交易型 data（如公司金融）直接取字段
            for field in fields:
                add_name(it.get(field))

    # 2) data 中未找到时，从 entity 兜底读取
    if not names:
        entity_raw = original.get('entity')
        entity_parsed = _parse_json_loose(entity_raw)
        if not isinstance(entity_parsed, list):
            entity_parsed = [entity_parsed] if entity_parsed else []
        for entry in entity_parsed:
            if not isinstance(entry, dict):
                continue
            if entry.get('main_tag') and entry.get('main_tag') != main_tag:
                continue
            entity_inner = entry.get('entity')
            if isinstance(entity_inner, str):
                entity_inner = _parse_json_loose(entity_inner)
            if not isinstance(entity_inner, (dict, list)):
                continue
            if isinstance(entity_inner, dict):
                entity_inner = [entity_inner]
            for it in entity_inner:
                if not isinstance(it, dict):
                    continue
                for field in fields + ("companies", "company"):
                    add_name(it.get(field))

    return names


async def _polish_english_with_ai(analyzer, draft, semaphore=None):
    """调用 LLM 对规则替换后的草稿进行英文润色。"""
    if not draft or not draft.strip():
        return draft
    prompt = ROUGH_TRANSLATION_PROMPT.format(draft=draft)
    try:
        if semaphore:
            async with semaphore:
                result = await analyzer.analyze(
                    content=draft,
                    prompt=prompt,
                    max_tokens=500,
                    model="qwen-plus",
                )
        else:
            result = await analyzer.analyze(
                content=draft,
                prompt=prompt,
                max_tokens=500,
                model="qwen-plus",
            )
        if result.get("success"):
            polished = str(result.get("analysis", "")).strip()
            if polished:
                return polished
    except Exception as e:
        print(f"   [WARN] 英文翻译润色失败: {e}")
    return draft


async def _translate_news_list_async(news_list, entity_dict, ship_dict):
    """为新闻列表批量生成英文翻译（结果写入 news_report_en 字段）。"""
    if not news_list:
        return
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("[WARN] 未设置 DASHSCOPE_API_KEY，跳过英文翻译")
        return

    try:
        analyzer = BailianWebAnalyzer(region="beijing")
    except SystemExit:
        print("[WARN] 创建 AI 分析器失败，跳过英文翻译")
        return

    semaphore = asyncio.Semaphore(5)

    async def _translate_one(news):
        report = news.get('news_report', '')
        # 同一卡片内可能有多篇换行拼接的报道，逐篇翻译后再按换行拼回，
        # 避免整段翻译时时间、主体等细节在报道之间错位
        parts = [p.strip() for p in str(report).split('\n') if p and p.strip()]
        if not parts:
            return
        translated = []
        for part in parts:
            draft = _apply_glossary_replacement(part, entity_dict, ship_dict)
            en = await _polish_english_with_ai(analyzer, draft, semaphore)
            translated.append(en if en else part)
        news['news_report_en'] = '\n'.join(translated)

    await asyncio.gather(*(_translate_one(n) for n in news_list), return_exceptions=True)


def _parse_target_date(target_date):
    """将字符串/日期对象统一转换为 date 对象，默认返回今天。"""
    if target_date is None:
        return datetime.now().date()
    if isinstance(target_date, str):
        try:
            return datetime.strptime(target_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            print(f"日期格式错误：{target_date}，应使用 YYYY-MM-DD，将使用今天")
            return datetime.now().date()
    if isinstance(target_date, datetime):
        return target_date.date()
    return target_date


def get_date_range_for_news(target_date=None):
    """返回用户指定的单个日期，默认为今天（不再按周末聚合）。"""
    target = _parse_target_date(target_date)
    print(f"目标日期：{target}（可在生成网页时选择其它日期）")
    return [target]


def _expand_news_report_array(item):
    """
    将 news_report 数组展开为多条独立新闻条目。
    兼容旧格式（字符串）。
    返回 [report_dict]，其中 report_dict 是 item 的浅拷贝并附加 news_report 和 main_tag。
    """
    import json
    news_report = item.get('news_report', '')
    if not news_report:
        return []

    if isinstance(news_report, str):
        try:
            report_groups = json.loads(news_report)
        except (json.JSONDecodeError, TypeError):
            # 旧格式：单条报告字符串
            main_tag = LEGACY_TO_MAIN_DIMENSION.get(
                _first_main_tag_from_item(item) or '', '行业新闻'
            )
            return [{
                'main_tag': main_tag,
                'news_report': news_report,
                '_original_item': item,
            }]
    else:
        report_groups = news_report

    if not isinstance(report_groups, list):
        main_tag = LEGACY_TO_MAIN_DIMENSION.get(
            _first_main_tag_from_item(item) or '', '行业新闻'
        )
        return [{
            'main_tag': main_tag,
            'news_report': str(news_report),
            '_original_item': item,
        }]

    expanded = []
    tx_cursor = {}  # 原始主标签 -> 已对齐到的交易下标
    for group in report_groups:
        if not isinstance(group, dict):
            continue
        # 跳过已标记为重复的报道
        if group.get('is_duplicate', False):
            continue
        main_tag = group.get('main_tag') or _first_main_tag_from_item(item) or '行业新闻'
        # 兼容历史数据中的旧主维度名称
        main_tag = LEGACY_TO_MAIN_DIMENSION.get(main_tag, main_tag)
        # 仅保留当前体系认可的主维度
        if main_tag not in MAIN_DIMENSION_CONFIG:
            main_tag = '行业新闻'
        report_text = group.get('report', '')
        if not report_text:
            continue
        # 与该报道对齐的交易：同一新闻同一主标签下，第 N 篇可见报道对应第 N 笔非重复交易
        raw_tag = str(group.get('main_tag') or '').strip()
        txs = _safe_main_tag_transactions(item, raw_tag)
        cursor = tx_cursor.get(raw_tag, 0)
        tx = txs[cursor] if cursor < len(txs) else None
        tx_cursor[raw_tag] = cursor + 1
        expanded.append({
            'main_tag': main_tag,
            'news_report': report_text,
            '_original_item': item,
            '_tx': tx,
        })

    return expanded


def _is_peripheral_news(item):
    """
    判断新闻是否应被过滤的"边缘新闻"。
    条件：main_tags 唯一且为 ["行业新闻"] 时，secondary_tags 包含"边缘新闻"。
    """
    tags = item.get('tags', '')
    if not tags:
        return False
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            return False
    if not isinstance(tags, dict):
        return False

    main_tags = tags.get('main_tags', [])
    if main_tags != ["行业新闻"]:
        return False

    secondary_tags = tags.get('secondary_tags', [])
    if isinstance(secondary_tags, str):
        secondary_tags = [secondary_tags]
    if not isinstance(secondary_tags, list):
        return False

    return "边缘新闻" in secondary_tags


def _data_field_non_empty(data, field):
    """递归检查 data（含主维度分组/交易数组嵌套）中指定字段是否存在非空值。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return False
    if isinstance(data, dict):
        if str(data.get(field) or '').strip():
            return True
        return any(_data_field_non_empty(v, field) for v in data.values() if isinstance(v, (dict, list)))
    if isinstance(data, list):
        return any(_data_field_non_empty(v, field) for v in data)
    return False


def _trade_news_has_money(item, tx):
    """
    买卖船/租船金额门槛：与该报道对齐的交易有 money（交易金额/租金），
    或整篇 data 存在汇总金额 all_price（未拆分到每笔的总额），才纳入最终报告；
    无交易对齐（data 缺失等）时退化为整篇是否出现任何 money/all_price。
    """
    if isinstance(tx, dict):
        if str(tx.get('money') or '').strip():
            return True
        return _data_field_non_empty(item.get('data'), 'all_price')
    return (_data_field_non_empty(item.get('data'), 'money')
            or _data_field_non_empty(item.get('data'), 'all_price'))


def extract_today_news_by_category(target_date=None, vpn_filter=None):
    """提取指定日期的新闻数据，并按主维度扁平分组

    参数：
        target_date: 目标日期
        vpn_filter: None 表示全部，"vpn" 表示仅需要 VPN 的平台，"no_vpn" 表示仅不需要 VPN 的平台
    """
    try:
        from db_config import DB_CONFIG_MYSQL
        conn = mysql.connector.connect(**DB_CONFIG_MYSQL)

        cursor = conn.cursor(dictionary=True)
        target_dates = get_date_range_for_news(target_date)

        date_conditions = " OR ".join(["date = %s"] * len(target_dates))
        query = f"""
        SELECT news_report, date, created_time,
               data, link, confidence_score, source_platform, tags
        FROM ship_news
        WHERE ({date_conditions})
        ORDER BY date DESC, id DESC
        """

        date_params = [date.strftime('%Y-%m-%d') for date in target_dates]
        cursor.execute(query, date_params)

        results = cursor.fetchall()

        # 根据 VPN 需求过滤（业务端希望导出时只包含指定平台类型）
        if vpn_filter == "vpn":
            results = [r for r in results if is_vpn_needed(r.get("source_platform", ""))]
        elif vpn_filter == "no_vpn":
            results = [r for r in results if not is_vpn_needed(r.get("source_platform", ""))]

        # 扁平结构：{main_dimension: [news, ...]}
        categorized_news = {}
        for item in results:
            expanded_items = _expand_news_report_array(item)
            if not expanded_items:
                continue

            for expanded in expanded_items:
                main_tag = expanded['main_tag']

                # 无法识别的主维度归入兜底
                if main_tag not in MAIN_DIMENSION_CONFIG:
                    main_tag = '行业新闻'

                # 跳过被标记为"边缘新闻"的报道（仅当 main_tags 唯一为行业新闻时生效）
                if _is_peripheral_news(item):
                    print(f"   🗑️ 跳过边缘新闻: {item.get('title', '')[:40]}...")
                    continue

                if main_tag not in categorized_news:
                    categorized_news[main_tag] = []

                # 构建最终渲染用的新闻条目
                render_item = item.copy()
                render_item['news_report'] = expanded['news_report']
                render_item['main_tag'] = main_tag
                render_item['_original_item'] = item
                render_item['_tx'] = expanded.get('_tx')

                # 买卖船/租船：无交易金额/租金信息的报道不纳入最终 HTML（无展示价值）
                if main_tag in ("买卖船", "租船") and not _trade_news_has_money(item, expanded.get('_tx')):
                    print(f"   💸 跳过无金额信息的{main_tag}报道: {str(expanded['news_report'])[:40]}...")
                    continue

                score = render_item.get('confidence_score')
                render_item['confidence_score'] = int(score) if score is not None else 0
                categorized_news[main_tag].append(render_item)

        # 每个主维度内按置信度从高到低排序
        for main_tag in categorized_news:
            categorized_news[main_tag].sort(
                key=lambda x: x['confidence_score'], reverse=True
            )

        cursor.close()
        conn.close()

        date_range_str = "、".join([date.strftime('%m月%d日') for date in target_dates])
        total = sum(len(items) for items in categorized_news.values())
        print(f"提取到 {total} 条新闻（{date_range_str}），共 {len(categorized_news)} 个主维度")
        return categorized_news

    except Exception as e:
        print(f"数据提取失败: {e}")
        return {}


def render_news_item(html_parts, news, index):
    """渲染单条新闻项（含中文报道与英文翻译）"""
    conf = news.get('confidence_score', 0)
    if conf >= 100:
        accurate_tag = '<span style="display: inline-block; background: #c0392b; color: #ffffff; padding: 2px 8px; font-size: 12px; font-weight: bold; border-radius: 4px;">V</span>&nbsp;'
    else:
        accurate_tag = '<span style="display: inline-block; background: #2980b9; color: #ffffff; padding: 2px 8px; font-size: 12px; font-weight: bold; border-radius: 4px;">AI</span>&nbsp;'

    cn_html = _format_report_html(news['news_report'])
    en_report = news.get('news_report_en', '')
    en_block = ''
    if en_report:
        en_html = _format_report_html(en_report)
        en_block = f'''<p style="font-size: 13px; color: #666; line-height: 1.8; margin: 12px 0 0 0; padding-top: 10px; border-top: 1px dashed #e0e0e0;">{en_html}</p>'''

    # 提取并渲染实体标签
    entities = _extract_entities_for_news(news)
    entity_block = ''
    if entities:
        entity_tags = ''.join(
            f'<span style="display: inline-block; background: #e6f7ed; color: #1e5e3e; padding: 3px 8px; margin: 0 4px 4px 0; font-size: 12px; border-radius: 10px; border: 1px solid #b8e0c8;">{html.escape(str(e))}</span>'
            for e in entities[:12]
        )
        entity_block = f'''<div style="margin: 6px 0 8px 0;">{entity_tags}</div>'''

    html_parts.append(f'''
    <section style="margin: 0 0 8px 14px; border-left: 3px solid #e8e8e8; padding-left: 12px;">
        <p style="font-size: 15px; color: #444; line-height: 1.9; margin: 0 0 8px 0;"><strong>{index}.</strong>&nbsp;{accurate_tag}{cn_html}</p>
        {entity_block}
        {en_block}
    </section>
''')


def _safe_main_tag_transactions(item, main_tag):
    """提取指定主标签下的交易列表（与报道生成使用同一解析逻辑），失败时返回空列表。"""
    if not main_tag:
        return []
    try:
        from ai_news_report_creating import _get_main_tag_transactions
        return _get_main_tag_transactions(item, main_tag)
    except Exception as e:
        print(f"[WARN] 提取交易列表失败 [{main_tag}]: {e}")
        return []


def _subject_names_from_transactions(transactions, main_tag):
    """主体标签只取自（被合并的）这些交易，而不是该新闻该主标签下的全部交易。"""
    fields = _CATEGORY_ENTITY_FIELDS.get(main_tag, ("shipowner", "shipyard"))
    names = []
    seen = set()
    for tx in transactions or []:
        if not isinstance(tx, dict):
            continue
        for field in fields:
            value = tx.get(field)
            items = value if isinstance(value, (list, tuple)) else [value]
            for n in items:
                s = str(n).strip() if n is not None else ''
                if not s or s.lower() in ("null", "none") or s in seen:
                    continue
                if is_broad_subject_value(s):
                    continue
                seen.add(s)
                names.append(s)
    return names[:12]


def _build_display_items(news_list):
    """
    把同一新闻、同一主标签下"相同主体"的多篇报道归入同一展示位：
    渲染时换行拼接进同一新闻卡片，主体标签也只取这些交易；
    其他交易、其他新闻完全不受影响。无法对齐交易的新闻保持原样。
    """
    # 先按原新闻归组，保持原新闻内报道顺序
    per_news = {}
    order = []
    for news in news_list:
        original = news.get('_original_item') or news
        oid = id(original)
        if oid not in per_news:
            per_news[oid] = []
            order.append(oid)
        per_news[oid].append(news)

    display = []
    for oid in order:
        entries = per_news[oid]
        by_tag = {}
        tag_order = []
        for e in entries:
            tag = e.get('main_tag', '')
            if tag not in by_tag:
                by_tag[tag] = []
                tag_order.append(tag)
            by_tag[tag].append(e)
        for tag in tag_order:
            tag_entries = by_tag[tag]
            fields = _CATEGORY_ENTITY_FIELDS.get(tag)
            txs = [e.get('_tx') for e in tag_entries]
            if not fields or any(t is None or not isinstance(t, dict) for t in txs):
                # 无法对齐交易（旧数据等）：每篇报道独立展示，主体标签走原提取逻辑
                for e in tag_entries:
                    display.append({'items': [e], 'txs': []})
                continue
            for group in group_transactions_by_shared_subject(txs, fields):
                member_ids = {id(t) for t in group}
                display.append({
                    'items': [e for e, t in zip(tag_entries, txs) if id(t) in member_ids],
                    'txs': list(group),
                })
    return display


def _join_reports_for_display(reports):
    """
    渲染阶段的处理：把同一主体（相同船东/买方/租家等）的多篇报道
    以换行拼接放进同一新闻卡片展示，各篇报道原文保持不变，
    不再用 AI 重新生成，避免时间等细节出现纰漏。
    只影响展示文本，不改数据库字段。
    """
    reports = [str(r).strip() for r in reports if r and str(r).strip()]
    return '\n'.join(reports)


async def _prepare_display_items_async(display_items, entity_dict, ship_dict):
    """先把相同主体的多篇报道换行拼接为一篇展示报道，再组装展示条目并批量生成英文翻译。"""
    if not display_items:
        return

    # 相同主体的多篇报道直接换行拼接，不调用 AI 重组
    for slot in display_items:
        reports = [it.get('news_report', '') for it in slot['items']]
        slot['report'] = _join_reports_for_display(reports)

    # 组装最终展示条目（原地替换 slot 内容，保持列表顺序）
    for slot in display_items:
        base = dict(slot['items'][0])
        base['news_report'] = slot.get('report') or slot['items'][0].get('news_report', '')
        # 主体标签只取自被合并的交易
        base['_entity_names'] = _subject_names_from_transactions(slot['txs'], base.get('main_tag', ''))
        scores = [it.get('confidence_score', 0) or 0 for it in slot['items']]
        base['confidence_score'] = max(scores) if scores else 0
        slot.clear()
        slot.update(base)

    await _translate_news_list_async(display_items, entity_dict, ship_dict)


def render_news_list(html_parts, news_list, start_index=1):
    """渲染新闻列表并在条目间添加分隔符；渲染前把同一主体（相同船东/买方等）
    的多篇报道换行拼接为一篇展示报道，并批量生成英文翻译。"""
    display_items = _build_display_items(news_list)
    entity_dict, ship_dict = _load_translation_glossaries()
    try:
        asyncio.run(_prepare_display_items_async(display_items, entity_dict, ship_dict))
    except RuntimeError as e:
        print(f"[WARN] 展示条目准备（重组/翻译）异步执行失败: {e}")

    for offset, news in enumerate(display_items):
        index = start_index + offset
        render_news_item(html_parts, news, index)
        if offset < len(display_items) - 1:
            html_parts.append('<p style="text-align: center; color: #ddd; font-size: 11px; margin: 10px 0; letter-spacing: 6px;">&bull;&nbsp;&nbsp;&bull;&nbsp;&nbsp;&bull;</p>')
    return start_index + len(display_items)


def generate_html_news(categorized_news, target_date=None):
    """生成 HTML 格式的新闻文档（按主维度扁平排版）"""

    target = _parse_target_date(target_date)
    title_date = target.strftime('%Y.%m.%d')

    # 按主维度优先级排序
    sorted_main_tags = sorted(
        categorized_news.keys(),
        key=lambda x: MAIN_DIMENSION_CONFIG.get(x, {}).get("priority", 99)
    )

    html_parts = []

    html_parts.append(f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>每日航运要闻 - {title_date}</title>
<style>
body {{ max-width: 680px; margin: 0 auto; padding: 20px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
</style>
</head>
<body>

<!-- ==== 从这里开始复制到公众号 ==== -->

<!-- 顶部装饰 -->
<section style="margin-bottom: 25px;">
    <div style="height: 3px; background: #2c3e50; margin-bottom: 6px;"></div>
    <div style="text-align: center;">
        <span style="display: inline-block; width: 50px; height: 3px; background: #8b7355;"></span>
    </div>
</section>

<!-- 标题区 -->
<section style="text-align: center; background: #f5f5f5; padding: 22px; border-left: 3px solid #2c3e50; border-right: 3px solid #2c3e50; margin-bottom: 28px;">
    <h2 style="font-size: 28px; font-weight: bold; color: #2c3e50; margin: 0 0 8px 0; line-height: 1.5; letter-spacing: 2px;">每日航运要闻</h2>
    <p style="font-size: 14px; color: #666; margin: 0; line-height: 1.7;">{title_date} | 聚焦全球航运市场动态</p>
</section>
''')

    first_tag = True
    for main_tag in sorted_main_tags:
        news_list = categorized_news[main_tag]
        if not news_list:
            continue

        tag_config = MAIN_DIMENSION_CONFIG.get(main_tag, {"priority": 99, "color": "#2c3e50"})
        tag_color = tag_config.get("color", "#2c3e50")

        if not first_tag:
            html_parts.append('''
<!-- 主维度分隔线 -->
<section style="text-align: center; margin: 28px 0;">
    <p style="color: #ccc; letter-spacing: 8px; margin: 0;">* * *</p>
</section>
''')
        first_tag = False

        # 主维度标题
        html_parts.append(f'''
<!-- {main_tag} -->
<section style="margin-bottom: 22px;">
    <p style="margin: 0 0 18px 0;">
        <span style="display: inline-block; background: {tag_color}; color: #ffffff; padding: 10px 24px; font-size: 16px; font-weight: bold; letter-spacing: 2px;">{main_tag}</span>
    </p>
''')
        render_news_list(html_parts, news_list, start_index=1)

        html_parts.append('''
</section>
''')

    # 页脚
    html_parts.append('''
<!-- 底部区域 -->
<section style="margin-top: 40px; padding-top: 30px; border-top: 1px solid #e0e0e0;">

    <!-- AI来源说明 -->
    <section style="background-color: #f0f7ff; padding: 20px; margin: 25px 0; border-left: 4px solid #2980b9;">
        <p style="font-size: 14px; font-weight: bold; color: #2980b9; margin: 0 0 12px 0; padding-bottom: 10px; border-bottom: 1px solid #d0e3f7;">ℹ 说明</p>
        <p style="font-size: 13px; color: #555; line-height: 1.9; margin: 0 0 8px 0;">本文档中的新闻内容均由 AI 自动从互联网获取、分析并整理撰写而成。</p>
        <p style="font-size: 13px; color: #777; line-height: 1.9; margin: 0;"><strong>免责申明：</strong>本内容仅供参考，不构成任何投资建议或决策依据。对于因使用本文档内容而产生的任何直接或间接损失，我方不承担任何法律责任。请读者自行核实相关信息并独立判断。</p>
    </section>

    <!-- 搬运声明 -->
    <section style="background-color: #fafafa; padding: 20px; margin: 25px 0;">
        <p style="font-size: 14px; font-weight: bold; color: #333; margin: 0 0 12px 0; padding-bottom: 10px; border-bottom: 1px solid #e0e0e0;">© 内容声明</p>
        <p style="font-size: 13px; color: #666; line-height: 1.9; margin: 0;">本文档为航运资讯汇编，内容来源于公开新闻报道，仅供行业交流参考。</p>
    </section>

</section>

<!-- ==== 复制到这里结束 ==== -->

</body>
</html>
''')

    return ''.join(html_parts)


def save_html_to_desktop(html_content, target_date=None):
    """保存HTML文件到项目输出目录"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.getenv('OUTPUT_DIR', os.path.join(base_dir, 'output'))
    news_folder = os.path.join(output_dir, "每日新闻")

    if not os.path.exists(news_folder):
        os.makedirs(news_folder)
        print(f"创建文件夹: {news_folder}")

    target = _parse_target_date(target_date)
    filename = f"每日新闻_{target.strftime('%Y%m%d')}.html"

    filepath = os.path.join(news_folder, filename)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"HTML文档已保存到: {filepath}")
    return filepath


def main(target_date=None, vpn_filter=None):
    """主执行函数"""
    target = _parse_target_date(target_date)
    print(f"开始生成 {target} 新闻HTML文档...")

    categorized_news = extract_today_news_by_category(target_date, vpn_filter=vpn_filter)

    if not categorized_news:
        print("指定日期范围内无新闻数据")
        return

    html_content = generate_html_news(categorized_news, target_date)
    filepath = save_html_to_desktop(html_content, target_date)

    target_dates = get_date_range_for_news(target_date)
    total_news = sum(len(items) for items in categorized_news.values())

    date_range_info = "、".join([date.strftime('%m月%d日') for date in target_dates])

    print(f"\n生成完成！")
    print(f"日期范围: {date_range_info}")
    print(f"文档包含 {len(categorized_news)} 个主维度，共 {total_news} 条新闻")

    for main_tag, news_list in categorized_news.items():
        print(f"> {main_tag}: {len(news_list)} 条")

    print(f"\n使用方法：")
    print(f"1. 用浏览器打开生成的HTML文件预览效果")
    print(f"2. 全选复制内容，粘贴到公众号编辑器")
    print(f"3. 或使用截图工具截取精美图片发布")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理 entity_registry.db 词表：
1. 删除宽泛/非特定实体名称（仅保留"地区+主体类别"格式）
2. 补齐 english_name
3. 将明显拼音的英文规范名翻译成中文（重复时删除）
4. 同步到 MySQL
"""
import json
import os
import re
import sqlite3
import sys
import time

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _BACKEND_DIR)

from app.api.correction_vocabulary import (
    _derive_english_name,
    _parse_aliases,
    _sync_entity_vocabulary_to_mysql,
)
from app.shipyard_ai.ai_extract_shipyard_info import call_qwen_api

DB_PATH = os.path.join(_BACKEND_DIR, "app", "news_engine", "data", "entity_registry.db")

def has_chinese(text: str) -> bool:
    return bool(re.search(r'[\u4e00-\u9fff]', text))

def is_latin(text: str) -> bool:
    if not text or not text.strip():
        return False
    return not bool(re.search(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]', text))

def _raw_load_entities():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM entities ORDER BY entity_type, canonical_name")
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        d['mapped_names'] = _parse_aliases(d.get('aliases'))
        rows.append(d)
    conn.close()
    return rows

def is_generic(name: str) -> bool:
    if not name:
        return False
    n = name.strip()
    lower = n.lower()
    if lower in ('null', 'none', ''):
        return True
    generic_markers = ['某', '未知', '未具名', '未明确', '有关', '相关', '各', '等', '一家', '某家', '多家', '第三方', '联合体', '投资者', '投资人', '相关方', '泛指', '客户', '知名']
    for m in generic_markers:
        if m in n:
            return True
    eng_generic_phrases = [
        'oil major', 'the company', 'the buyer', 'the seller', 'the shipowner',
        'the shipyard', 'the charterer', 'the owner', 'the parties',
        'unnamed', 'unknown', 'third-party investors', 'joint venture (third-party',
        'third party investors', 'investor consortium', 'investor group',
        '-led ', 'shareholders', 'subsidiary of', 'affiliate of',
    ]
    for phrase in eng_generic_phrases:
        if phrase in lower:
            return True
    country_only_pattern = r'^(中国|韩国|日本|美国|欧洲|亚洲|中东|俄罗斯|乌克兰|德国|法国|英国|挪威|希腊|新加坡|马来西亚|印尼|印度|巴西|土耳其|越南|菲律宾|泰国|埃及|南非|澳洲|澳大利亚|加拿大|意大利|西班牙|荷兰|比利时|瑞典|芬兰|丹麦|波兰|罗马尼亚|保加利亚|克罗地亚|塞尔维亚|阿根廷|智利|墨西哥|阿联酋|沙特|伊朗|以色列|巴基斯坦|孟加拉|斯里兰卡|缅甸|柬埔寨|老挝|尼泊尔|蒙古)$'
    if re.match(country_only_pattern, n):
        return True
    pure_type_pattern = r'^(船厂|船东|造船厂|造船企业|航运公司|航运企业|海运公司|海运企业|出租方|承租方|买方|卖方|买家|卖家|采购方|供应方|供应商|贸易商|船公司|船级社|银行|金融机构|投资公司|租赁公司|船务公司|海事公司|能源公司|石油公司|天然气公司|物流公司|工程公司|咨询公司|经纪公司|港口|码头|船坞|公司|集团)$'
    if re.match(pure_type_pattern, n):
        return True
    descriptive_patterns = [
        r'^Chinese\s+(lessor|controlled|buyer|seller|shipowner|shipyard|charterer|company|investor)',
        r'^(lessor|controlled)$',
    ]
    for pat in descriptive_patterns:
        if re.search(pat, n, re.IGNORECASE):
            return True
    return False

def is_allowed_region_type(name: str) -> bool:
    if not name:
        return False
    n = name.strip()
    region_pattern = r'^(中国|韩国|日本|美国|欧洲|亚洲|中东|俄罗斯|乌克兰|德国|法国|英国|挪威|希腊|新加坡|马来西亚|印尼|印度尼西亚|印度|巴西|土耳其|越南|菲律宾|泰国|埃及|南非|澳洲|澳大利亚|加拿大|意大利|西班牙|荷兰|比利时|瑞典|芬兰|丹麦|波兰|罗马尼亚|保加利亚|克罗地亚|塞尔维亚|阿根廷|智利|墨西哥|阿联酋|沙特|伊朗|以色列|巴基斯坦|孟加拉|斯里兰卡|缅甸|柬埔寨|老挝|尼泊尔|蒙古|哈萨克斯坦|乌兹别克|吉尔吉斯|塔吉克|土库曼|阿塞拜疆|格鲁吉亚|亚美尼亚|白俄罗斯|摩尔多瓦|立陶宛|拉脱维亚|爱沙尼亚|斯洛伐克|捷克|匈牙利|斯洛文尼亚|波黑|黑山|北马其顿|阿尔巴尼亚|科索沃|塞浦路斯|马耳他|冰岛|爱尔兰|卢森堡|瑞士|奥地利|葡萄牙|国内|国外|海外|境外|外部|内部|当地|本土|国际)?(船厂|船东|造船厂|造船企业|航运公司|航运企业|海运公司|海运企业|出租方|承租方|买方|卖方|买家|卖家|采购方|供应方|供应商|贸易商|船公司|船级社|银行|金融机构|投资公司|租赁公司|船务公司|海事公司|能源公司|石油公司|天然气公司|物流公司|工程公司|咨询公司|经纪公司|港口|码头|船坞)$'
    return bool(re.match(region_pattern, n))

def should_delete(row: dict) -> bool:
    canonical = row['canonical_name']
    if is_allowed_region_type(canonical):
        return False
    return is_generic(canonical)

def is_obviously_pinyin(name: str) -> bool:
    if not name or not is_latin(name):
        return False
    obvious_pinyin = {
        'Chuan Bo Maritime',
        'Ming Yang Smart Energy Group',
        'Ming Yang Smart Energy',
    }
    if name in obvious_pinyin:
        return True
    n = name.strip()
    suffixes = ['Shipping', 'Shipyard', 'Maritime', 'Shipbuilding', 'Marine', 'Lines', 'Group', 'Company', 'Co.', 'Ltd', 'Limited', 'Inc', 'Corporation']
    core = n
    for suf in suffixes:
        core = re.sub(r'\b' + re.escape(suf) + r'\b', '', core, flags=re.IGNORECASE)
    core = re.sub(r'[^a-zA-Z\s]', '', core).strip()
    if not core:
        return False
    words = core.split()
    if len(words) < 2:
        return False
    pinyin = set([
        'a','ai','an','ang','ao','ba','bai','ban','bang','bao','bei','ben','beng','bi','bian','biao','bie','bin','bing','bo','bu',
        'ca','cai','can','cang','cao','ce','cen','ceng','cha','chai','chan','chang','chao','che','chen','cheng','chi','chong','chou','chu','chuan','chuang','chui','chun','chuo','ci','cong','cou','cu','cuan','cui','cun','cuo',
        'da','dai','dan','dang','dao','de','deng','di','dian','diao','die','ding','diu','dong','dou','du','duan','dui','dun','duo',
        'e','en','er','fa','fan','fang','fei','fen','feng','fo','fou','fu','ga','gai','gan','gang','gao','ge','gei','gen','geng','gong','gou','gu','gua','guai','guan','guang','gui','gun','guo',
        'ha','hai','han','hang','hao','he','hei','hen','heng','hong','hou','hu','hua','huai','huan','huang','hui','hun','huo','ji','jia','jian','jiang','jiao','jie','jin','jing','jiong','jiu','ju','juan','jue','jun',
        'ka','kai','kan','kang','kao','ke','ken','keng','kong','kou','ku','kua','kuai','kuan','kuang','kui','kun','kuo','la','lai','lan','lang','lao','le','lei','leng','li','lian','liang','liao','lie','lin','ling','liu','long','lou','lu','luan','lve','lun','luo','ma','mai','man','mang','mao','me','mei','men','meng','mi','mian','miao','mie','min','ming','miu','mo','mou','mu',
        'na','nai','nan','nang','nao','ne','nei','nen','neng','ni','nian','niang','niao','nie','nin','ning','niu','nong','nou','nu','nuan','nve','nuo','o','ou','pa','pai','pan','pang','pao','pei','pen','peng','pi','pian','piao','pie','pin','ping','po','pou','pu',
        'qi','qia','qian','qiang','qiao','qie','qin','qing','qiong','qiu','qu','quan','que','qun','ran','rang','rao','re','ren','reng','ri','rong','rou','ru','ruan','rui','run','ruo','sa','sai','san','sang','sao','se','sen','seng','sha','shai','shan','shang','shao','she','shei','shen','sheng','shi','shou','shu','shua','shuai','shuan','shuang','shui','shun','shuo','si','song','sou','su','suan','sui','sun','suo',
        'ta','tai','tan','tang','tao','te','teng','ti','tian','tiao','tie','ting','tong','tou','tu','tuan','tui','tun','tuo','wa','wai','wan','wang','wei','wen','weng','wo','wu','xi','xia','xian','xiang','xiao','xie','xin','xing','xiong','xiu','xu','xuan','xue','xun',
        'ya','yan','yang','yao','ye','yi','yin','ying','yo','yong','you','yu','yuan','yue','yun','za','zai','zan','zang','zao','ze','zei','zen','zeng','zha','zhai','zhan','zhang','zhao','zhe','zhei','zhen','zheng','zhi','zhong','zhou','zhu','zhua','zhuai','zhuan','zhuang','zhui','zhun','zhuo','zi','zong','zou','zu','zuan','zui','zun','zuo'
    ])
    def split_words(w):
        return re.findall(r'[A-Z][a-z]+|[a-z]+', w)
    total = 0
    pinyin_count = 0
    for w in words:
        parts = split_words(w)
        if not parts:
            parts = [w]
        for p in parts:
            p = p.lower()
            if len(p) <= 1:
                continue
            total += 1
            if p in pinyin:
                pinyin_count += 1
    return total >= 2 and pinyin_count == total

def translate_pinyin_names(pinyin_rows: list) -> dict:
    if not pinyin_rows:
        return {}
    items = [{'original': r['canonical_name'], 'entity_type': r['entity_type'], 'aliases': r.get('mapped_names', [])} for r in pinyin_rows]
    prompt = (
        "将以下英文规范名中明显是汉语拼音的海运/船舶行业实体名称翻译为中文规范名。"
        "如果原始名称已经是国际通用英文名，请保持原样不要翻译。"
        "只翻译那些明显由拼音音节组成、对应中国企业的名称。"
        "返回 JSON 数组，每个元素包含 original 和 chinese 两个字段。\n\n"
        + json.dumps(items, ensure_ascii=False, indent=2)
        + "\n\n只输出 JSON，不要解释。"
    )
    result = call_qwen_api(prompt, "", model="qwen-plus", timeout=120)
    if not result:
        return {}
    try:
        text = result.strip()
        if '```' in text:
            parts = text.split('```')
            text = parts[1] if len(parts) > 1 else text
            if text.startswith('json'):
                text = text[4:]
        data = json.loads(text.strip())
        return {item['original']: item['chinese'] for item in data if item.get('chinese') and item['chinese'] != item['original']}
    except Exception as e:
        print(f"[WARN] 拼音翻译解析失败: {e}\n原始结果: {result}")
        return {}

def translate_chinese_names(chinese_rows: list) -> dict:
    if not chinese_rows:
        return {}
    items = [{'chinese': r['canonical_name'], 'entity_type': r['entity_type'], 'aliases': r.get('mapped_names', [])} for r in chinese_rows]
    prompt = (
        "将以下中文实体名称翻译为英文规范名。要求：\n"
        "1. 对于国际知名公司（如三星重工、三菱电机、中远海运、壳牌、埃克森美孚等），使用其官方标准英文名。\n"
        "2. 对于中国公司，使用拼音或直译，保留地区名拼音（如上海=Shanghai，江苏=Jiangsu）。\n"
        "3. 对于地区+主体类别的宽泛名称（如中国买家、韩国船厂），翻译成 'Region + Entity Type' 格式（如 Chinese Buyer, Korean Shipyard）。\n"
        "4. 返回 JSON 数组，每个元素包含 chinese 和 english 两个字段。\n\n"
        + json.dumps(items, ensure_ascii=False, indent=2)
        + "\n\n只输出 JSON，不要解释。"
    )
    result = call_qwen_api(prompt, "", model="qwen-plus", timeout=120)
    if not result:
        return {}
    try:
        text = result.strip()
        if '```' in text:
            parts = text.split('```')
            text = parts[1] if len(parts) > 1 else text
            if text.startswith('json'):
                text = text[4:]
        data = json.loads(text.strip())
        return {item['chinese']: item['english'] for item in data if item.get('english')}
    except Exception as e:
        print(f"[WARN] 中文翻译解析失败: {e}\n原始结果: {result}")
        return {}

def main():
    print("[1/6] 加载实体词表...")
    rows = _raw_load_entities()
    print(f"      共 {len(rows)} 条")

    print("[2/6] 识别需要删除/处理的记录...")
    to_delete = []
    english_to_fill = []
    chinese_to_translate = []
    pinyin_to_translate = []

    for r in rows:
        canonical = r['canonical_name']
        english = (r.get('english_name') or '').strip()

        if should_delete(r):
            to_delete.append(r)
            continue

        if is_latin(canonical):
            if not english:
                english_to_fill.append({**r, '_new_english': canonical})
            if is_obviously_pinyin(canonical):
                pinyin_to_translate.append(r)
        else:
            if not english:
                derived = _derive_english_name(canonical, r.get('mapped_names', []))
                if derived:
                    english_to_fill.append({**r, '_new_english': derived})
                else:
                    chinese_to_translate.append(r)

    pinyin_ids = {r['id'] for r in pinyin_to_translate}
    english_to_fill = [r for r in english_to_fill if r['id'] not in pinyin_ids]

    print(f"      删除: {len(to_delete)}")
    print(f"      补全/修正 english_name: {len(english_to_fill)}")
    print(f"      拼音转中文: {len(pinyin_to_translate)}")
    print(f"      中文需LLM翻译: {len(chinese_to_translate)}")

    if to_delete:
        print("\n将删除的记录:")
        for r in to_delete:
            print(f"  [{r['entity_type']}] {r['canonical_name']}")

    if pinyin_to_translate:
        print("\n[3/6] 翻译拼音名称...")
        pinyin_map = translate_pinyin_names(pinyin_to_translate)
        print(f"      翻译结果: {pinyin_map}")
    else:
        pinyin_map = {}

    if chinese_to_translate:
        print("\n[4/6] 批量翻译中文名称...")
        batch_size = 80
        chinese_map = {}
        total_batches = (len(chinese_to_translate) + batch_size - 1) // batch_size
        for i in range(0, len(chinese_to_translate), batch_size):
            batch = chinese_to_translate[i:i+batch_size]
            print(f"      批次 {i//batch_size + 1}/{total_batches} ({len(batch)} 条)...")
            batch_map = translate_chinese_names(batch)
            chinese_map.update(batch_map)
            if i + batch_size < len(chinese_to_translate):
                time.sleep(1)
        print(f"      翻译成功: {len(chinese_map)}/{len(chinese_to_translate)}")
        untranslated = [r for r in chinese_to_translate if r['canonical_name'] not in chinese_map]
        if untranslated:
            print(f"      未翻译: {len(untranslated)} 条")
            for r in untranslated[:10]:
                print(f"        [{r['entity_type']}] {r['canonical_name']}")
    else:
        chinese_map = {}

    print("\n[5/6] 应用修改到 SQLite...")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        deleted_ids = [r['id'] for r in to_delete]
        if deleted_ids:
            cur.execute(f"DELETE FROM entities WHERE id IN ({','.join('?'*len(deleted_ids))})", deleted_ids)
            print(f"      已删除 {cur.rowcount} 条")

        update_count = 0
        for r in english_to_fill:
            new_english = r.get('_new_english', '')
            if new_english:
                cur.execute(
                    "UPDATE entities SET english_name = ? WHERE id = ?",
                    (new_english, r['id'])
                )
                update_count += 1

        pinyin_updated = 0
        pinyin_deleted = 0
        for r in pinyin_to_translate:
            orig = r['canonical_name']
            chinese = pinyin_map.get(orig)
            if not chinese:
                continue
            cur.execute(
                "SELECT id FROM entities WHERE entity_type = ? AND canonical_name = ?",
                (r['entity_type'], chinese)
            )
            existing = cur.fetchone()
            if existing:
                cur.execute("DELETE FROM entities WHERE id = ?", (r['id'],))
                pinyin_deleted += 1
            else:
                cur.execute(
                    "UPDATE entities SET canonical_name = ?, english_name = ? WHERE id = ?",
                    (chinese, orig, r['id'])
                )
                pinyin_updated += 1
        print(f"      拼音转中文: 更新 {pinyin_updated} 条, 删除重复 {pinyin_deleted} 条")

        chinese_updated = 0
        for r in chinese_to_translate:
            cn = r['canonical_name']
            eng = chinese_map.get(cn)
            if eng:
                cur.execute(
                    "UPDATE entities SET english_name = ? WHERE id = ?",
                    (eng, r['id'])
                )
                chinese_updated += 1
        print(f"      中文翻译: 更新 {chinese_updated} 条")
        print(f"      英文补全: 更新 {update_count} 条")

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("\n[6/6] 同步到 MySQL...")
    _sync_entity_vocabulary_to_mysql()
    print("      同步完成")

    print("\n处理完成。")

if __name__ == '__main__':
    main()

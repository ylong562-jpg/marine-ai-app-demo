# -*- coding: utf-8 -*-
"""
构建完整的船型层级分类表。

输入：
- 网上搜索整理的标准船型分类
- 无监督分类/vocabularies/ship_type_vocabulary.json 中从新闻提取的船型
- news_ai/data/船型名称表.xlsx 中的船型映射

输出：
- back_up/news_ai/无监督分类/vocabularies/ship_type_taxonomy.json
"""

import sys
import os
import json
import re
from pathlib import Path
from typing import Dict, List, Set, Any
from openai import OpenAI

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TARGET_DIR = Path(__file__).parent / "back_up" / "news_ai" / "无监督分类"
INPUT_VOCAB = TARGET_DIR / "vocabularies" / "ship_type_vocabulary.json"
OUTPUT_TAXONOMY = TARGET_DIR / "vocabularies" / "ship_type_taxonomy.json"

# 基于网络搜索和航运行业标准构建的完整船型层级分类
# 每个条目：标准中文名、英文别名/简称、父级类别、DWT/容量范围（可选）
BASE_TAXONOMY = [
    # ========== 集装箱船 ==========
    {"category": "集装箱船", "canonical_name": "小型支线集装箱船", "aliases": ["Small Feeder", "feeder"], "size": "< 1000 TEU", "parent": "集装箱船"},
    {"category": "集装箱船", "canonical_name": "支线集装箱船", "aliases": ["Feeder", "Feedermax"], "size": "1000-2000 TEU", "parent": "集装箱船"},
    {"category": "集装箱船", "canonical_name": "巴拿马型集装箱船", "aliases": ["Panamax Container Ship", "Panamax"], "size": "3000-5100 TEU", "parent": "集装箱船"},
    {"category": "集装箱船", "canonical_name": "超巴拿马型集装箱船", "aliases": ["Post-Panamax"], "size": "5100-10000 TEU", "parent": "集装箱船"},
    {"category": "集装箱船", "canonical_name": "新巴拿马型集装箱船", "aliases": ["Neo-Panamax"], "size": "10000-14500 TEU", "parent": "集装箱船"},
    {"category": "集装箱船", "canonical_name": "超大型集装箱船", "aliases": ["ULCV", "Ultra Large Container Vessel"], "size": "> 14500 TEU", "parent": "集装箱船"},

    # ========== 散货船 ==========
    {"category": "散货船", "canonical_name": "小型散货船", "aliases": ["Mini Bulker"], "size": "< 10000 DWT", "parent": "散货船"},
    {"category": "散货船", "canonical_name": "灵便型散货船", "aliases": ["Handysize", "Handy-size", "Handysize Bulk Carrier"], "size": "10000-39999 DWT", "parent": "散货船"},
    {"category": "散货船", "canonical_name": "大灵便型散货船", "aliases": ["Handymax", "Handymax Bulk Carrier"], "size": "35000-49999 DWT", "parent": "散货船"},
    {"category": "散货船", "canonical_name": "超灵便型散货船", "aliases": ["Supramax", "Ultramax"], "size": "50000-64999 DWT", "parent": "散货船"},
    {"category": "散货船", "canonical_name": "巴拿马型散货船", "aliases": ["Panamax Bulk Carrier", "Kamsarmax"], "size": "65000-99999 DWT", "parent": "散货船"},
    {"category": "散货船", "canonical_name": "后巴拿马型散货船", "aliases": ["Post-Panamax Bulk Carrier"], "size": "100000-119999 DWT", "parent": "散货船"},
    {"category": "散货船", "canonical_name": "好望角型散货船", "aliases": ["Capesize", "海岬型散货船"], "size": "120000-199999 DWT", "parent": "散货船"},
    {"category": "散货船", "canonical_name": "纽卡斯尔型散货船", "aliases": ["Newcastlemax"], "size": "200000-209999 DWT", "parent": "散货船"},
    {"category": "散货船", "canonical_name": "超大型矿砂船", "aliases": ["VLOC", "Very Large Ore Carrier", "Valemax", "Chinamax"], "size": ">= 200000 DWT", "parent": "散货船"},

    # ========== 油轮 ==========
    # 原油轮
    {"category": "油轮", "canonical_name": "超大型原油船", "aliases": ["VLCC", "Very Large Crude Carrier"], "size": "200000-319999 DWT", "parent": "原油轮"},
    {"category": "油轮", "canonical_name": "巨型原油船", "aliases": ["ULCC", "Ultra Large Crude Carrier"], "size": ">= 320000 DWT", "parent": "原油轮"},
    {"category": "油轮", "canonical_name": "苏伊士型油轮", "aliases": ["Suezmax", "Suezmax Tanker"], "size": "120000-199999 DWT", "parent": "原油轮"},
    {"category": "油轮", "canonical_name": "阿芙拉型油轮", "aliases": ["Aframax"], "size": "80000-119999 DWT", "parent": "原油轮"},
    {"category": "油轮", "canonical_name": "巴拿马型油轮", "aliases": ["Panamax Tanker", "LR2", "Long Range 2"], "size": "60000-79999 DWT", "parent": "原油轮/成品油轮"},
    # 成品油轮
    {"category": "油轮", "canonical_name": "远程1型成品油轮", "aliases": ["LR1", "Long Range 1"], "size": "45000-79999 DWT", "parent": "成品油轮"},
    {"category": "油轮", "canonical_name": "远程2型成品油轮", "aliases": ["LR2", "Long Range 2", "Panamax Product Tanker"], "size": "80000-119999 DWT", "parent": "成品油轮"},
    {"category": "油轮", "canonical_name": "中程成品油轮", "aliases": ["MR", "Medium Range", "MR1", "MR2"], "size": "25000-54999 DWT", "parent": "成品油轮"},
    {"category": "油轮", "canonical_name": "灵便型成品油轮", "aliases": ["Handy Product Tanker", "General Purpose", "GP"], "size": "10000-24999 DWT", "parent": "成品油轮"},
    {"category": "油轮", "canonical_name": "小型成品油轮", "aliases": ["Small Product Tanker"], "size": "< 10000 DWT", "parent": "成品油轮"},
    # 化学品船
    {"category": "油轮", "canonical_name": "化学品船", "aliases": ["Chemical Tanker", "IMO Type 1 Chemical Tanker", "IMO Type 2 Chemical Tanker", "IMO Type 3 Chemical Tanker"], "size": " varied", "parent": "化学品船"},
    {"category": "油轮", "canonical_name": "沥青运输船", "aliases": ["Bitumen Tanker", "Asphalt Carrier"], "size": "varied", "parent": "特种液货船"},
    {"category": "油轮", "canonical_name": "植物油运输船", "aliases": ["Vegetable Oil Tanker", "Edible Oil Tanker"], "size": "varied", "parent": "特种液货船"},

    # ========== 液化气运输船 ==========
    {"category": "液化气运输船", "canonical_name": "液化天然气运输船", "aliases": ["LNG Carrier", "LNG船", "液化天然气船"], "size": "varied CBM", "parent": "液化气运输船"},
    {"category": "液化气运输船", "canonical_name": "Q-Max型LNG船", "aliases": ["Q-Max"], "size": "~266000 CBM", "parent": "液化天然气运输船"},
    {"category": "液化气运输船", "canonical_name": "Q-Flex型LNG船", "aliases": ["Q-Flex"], "size": "~210000 CBM", "parent": "液化天然气运输船"},
    {"category": "液化气运输船", "canonical_name": "大型常规LNG船", "aliases": ["Large Conventional LNG Carrier"], "size": "150000-180000 CBM", "parent": "液化天然气运输船"},
    {"category": "液化气运输船", "canonical_name": "小型LNG船", "aliases": ["Small-scale LNG Carrier"], "size": "< 40000 CBM", "parent": "液化天然气运输船"},
    {"category": "液化气运输船", "canonical_name": "液化石油气运输船", "aliases": ["LPG Carrier", "LPG船", "液化石油气船"], "size": "varied CBM", "parent": "液化气运输船"},
    {"category": "液化气运输船", "canonical_name": "超大型液化石油气船", "aliases": ["VLGC", "Very Large Gas Carrier"], "size": "> 60000 CBM", "parent": "液化石油气运输船"},
    {"category": "液化气运输船", "canonical_name": "大型液化石油气船", "aliases": ["LGC", "Large Gas Carrier"], "size": "40000-60000 CBM", "parent": "液化石油气运输船"},
    {"category": "液化气运输船", "canonical_name": "中型液化石油气船", "aliases": ["MGC", "Medium Gas Carrier"], "size": "20000-39999 CBM", "parent": "液化石油气运输船"},
    {"category": "液化气运输船", "canonical_name": "乙烷运输船", "aliases": ["Ethane Carrier", "VLEC", "Very Large Ethane Carrier"], "size": "varied CBM", "parent": "液化气运输船"},
    {"category": "液化气运输船", "canonical_name": "氨运输船", "aliases": ["Ammonia Carrier", "氨燃料加注船"], "size": "varied CBM", "parent": "液化气运输船"},
    {"category": "液化气运输船", "canonical_name": "二氧化碳运输船", "aliases": ["CO2 Carrier"], "size": "varied CBM", "parent": "液化气运输船"},

    # ========== 汽车/滚装船 ==========
    {"category": "汽车运输船", "canonical_name": "汽车运输船", "aliases": ["Car Carrier", "PCC", "Pure Car Carrier"], "size": "varied CEU", "parent": "汽车运输船"},
    {"category": "汽车运输船", "canonical_name": "汽车/卡车运输船", "aliases": ["PCTC", "Pure Car and Truck Carrier", "Vehicle Carrier"], "size": "varied CEU", "parent": "汽车运输船"},
    {"category": "滚装船", "canonical_name": "滚装船", "aliases": ["Ro-Ro", "Roll-on/Roll-off", "RoRo Ship"], "size": "varied", "parent": "滚装船"},
    {"category": "滚装船", "canonical_name": "客滚船", "aliases": ["Ro-Pax", "RoPax"], "size": "varied", "parent": "滚装船"},
    {"category": "滚装船", "canonical_name": "渡轮", "aliases": ["Ferry"], "size": "varied", "parent": "滚装船"},
    {"category": "滚装船", "canonical_name": "邮轮", "aliases": ["Cruise Ship", "游轮"], "size": "varied GT", "parent": "客船"},

    # ========== 多用途/重吊 ==========
    {"category": "多用途船", "canonical_name": "多用途船", "aliases": ["MPP", "Multi-purpose Vessel", "MPV", "Multi-purpose Carrier"], "size": "varied", "parent": "多用途船"},
    {"category": "多用途船", "canonical_name": "重吊船", "aliases": ["Heavy Lift Vessel", "Heavy Lift Ship", "HLV"], "size": "varied", "parent": "多用途船"},
    {"category": "多用途船", "canonical_name": "杂货船", "aliases": ["General Cargo Ship", "General Cargo Vessel"], "size": "varied", "parent": "杂货船"},

    # ========== 海工平台 ==========
    {"category": "海工平台", "canonical_name": "浮式生产储卸油装置", "aliases": ["FPSO", "Floating Production Storage and Offloading"], "size": "varied", "parent": "海工平台"},
    {"category": "海工平台", "canonical_name": "浮式液化天然气装置", "aliases": ["FLNG", "Floating LNG"], "size": "varied", "parent": "海工平台"},
    {"category": "海工平台", "canonical_name": "浮式储存再气化装置", "aliases": ["FSRU", "Floating Storage Regasification Unit"], "size": "varied", "parent": "海工平台"},
    {"category": "海工平台", "canonical_name": "半潜式钻井平台", "aliases": ["Semi-submersible", "Semi-submersible Rig"], "size": "varied", "parent": "海工平台"},
    {"category": "海工平台", "canonical_name": "自升式钻井平台", "aliases": ["Jack-up Rig", "Jack-up Platform", "自升式平台"], "size": "varied", "parent": "海工平台"},
    {"category": "海工平台", "canonical_name": "钻井船", "aliases": ["Drillship"], "size": "varied", "parent": "海工平台"},
    {"category": "海工平台", "canonical_name": "半潜船", "aliases": ["Semi-submersible Ship", "Semi-submersible Vessel"], "size": "varied", "parent": "海工平台"},

    # ========== 海工辅助船 ==========
    {"category": "海工辅助船", "canonical_name": "平台供应船", "aliases": ["PSV", "Platform Supply Vessel"], "size": "varied", "parent": "海工辅助船"},
    {"category": "海工辅助船", "canonical_name": "三用工作船", "aliases": ["AHTS", "Anchor Handling Tug Supply", "AHTSS"], "size": "varied", "parent": "海工辅助船"},
    {"category": "海工辅助船", "canonical_name": "海缆敷设船", "aliases": ["Cable Layer", "Cable Ship", "CLV", "Cable Laying Vessel"], "size": "varied", "parent": "海工辅助船"},
    {"category": "海工辅助船", "canonical_name": "铺管船", "aliases": ["Pipe-laying Vessel", "Pipe Layer"], "size": "varied", "parent": "海工辅助船"},
    {"category": "海工辅助船", "canonical_name": "风电运维船", "aliases": ["CTV", "Crew Transfer Vessel", "SOV", "Service Operation Vessel"], "size": "varied", "parent": "海工辅助船"},

    # ========== 特种船 ==========
    {"category": "特种船", "canonical_name": "冷藏船", "aliases": ["Reefer Ship", "Refrigerated Cargo Ship"], "size": "varied", "parent": "特种船"},
    {"category": "特种船", "canonical_name": "牲畜运输船", "aliases": ["Livestock Carrier"], "size": "varied", "parent": "特种船"},
    {"category": "特种船", "canonical_name": "木屑船", "aliases": ["Woodchip Carrier"], "size": "varied", "parent": "特种船"},
    {"category": "特种船", "canonical_name": "水泥船", "aliases": ["Cement Carrier"], "size": "varied", "parent": "特种船"},
    {"category": "特种船", "canonical_name": "挖泥船", "aliases": ["Dredger", "Dredging Vessel"], "size": "varied", "parent": "工程船"},
    {"category": "特种船", "canonical_name": "起重船", "aliases": ["Crane Ship", "Floating Crane"], "size": "varied", "parent": "工程船"},
    {"category": "特种船", "canonical_name": "拖轮", "aliases": ["Tug", "Tugboat"], "size": "varied", "parent": "港作船"},
    {"category": "特种船", "canonical_name": "救助船", "aliases": ["Rescue Vessel", "Salvage Ship"], "size": "varied", "parent": "特种船"},
    {"category": "特种船", "canonical_name": "破冰船", "aliases": ["Icebreaker"], "size": "varied", "parent": "特种船"},
    {"category": "特种船", "canonical_name": "科考船", "aliases": ["Research Vessel"], "size": "varied", "parent": "特种船"},
]


def load_extracted_vocab() -> List[Dict]:
    if INPUT_VOCAB.exists():
        with open(INPUT_VOCAB, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def normalize_name(name: str) -> str:
    """标准化名称用于去重比较"""
    name = name.strip().lower()
    name = re.sub(r"[\s\-_/]+", "", name)
    return name


def build_taxonomy() -> Dict[str, Any]:
    """构建层级分类表"""
    # 顶层分类
    top_categories = {
        "集装箱船": {"subcategories": []},
        "散货船": {"subcategories": []},
        "油轮": {"subcategories": []},
        "液化气运输船": {"subcategories": []},
        "汽车运输船": {"subcategories": []},
        "滚装船": {"subcategories": []},
        "多用途船": {"subcategories": []},
        "海工辅助船": {"subcategories": []},
        "海工平台": {"subcategories": []},
        "特种船": {"subcategories": []},
        "其他": {"subcategories": []},
    }

    # 中间层级映射
    mid_levels = {
        "原油轮": "油轮",
        "成品油轮": "油轮",
        "化学品船": "油轮",
        "特种液货船": "油轮",
        "液化天然气运输船": "液化气运输船",
        "液化石油气运输船": "液化气运输船",
        "海工辅助船": "海工辅助船",
        "海工平台": "海工平台",
        "客船": "滚装船",
        "杂货船": "多用途船",
        "工程船": "特种船",
        "港作船": "特种船",
    }

    result = {
        "taxonomy": top_categories,
        "mid_levels": mid_levels,
        "ship_types": []
    }

    # 用于去重
    seen_canonical = set()
    seen_aliases = {}

    for item in BASE_TAXONOMY:
        cat = item["category"]
        name = item["canonical_name"]
        aliases = item.get("aliases", [])
        parent = item.get("parent", cat)
        size = item.get("size", "")

        norm = normalize_name(name)
        if norm in seen_canonical:
            continue
        seen_canonical.add(norm)

        # 确保别名不重复
        unique_aliases = []
        for a in aliases:
            na = normalize_name(a)
            if na and na != norm and na not in seen_aliases:
                seen_aliases[na] = name
                unique_aliases.append(a)

        result["ship_types"].append({
            "canonical_name": name,
            "aliases": unique_aliases,
            "category": cat,
            "parent": parent,
            "size": size,
            "source": "industry_standard"
        })

    return result


def merge_extracted(taxonomy: Dict, extracted: List[Dict]) -> Dict:
    """合并从新闻中提取的船型"""
    seen = set(normalize_name(s["canonical_name"]) for s in taxonomy["ship_types"])
    seen_aliases = set()
    for s in taxonomy["ship_types"]:
        for a in s["aliases"]:
            seen_aliases.add(normalize_name(a))

    for item in extracted:
        name = item.get("canonical_name", "")
        if not name:
            continue
        norm = normalize_name(name)

        # 如果已存在，跳过或合并别名
        if norm in seen:
            # 找到已有条目，合并别名
            for existing in taxonomy["ship_types"]:
                if normalize_name(existing["canonical_name"]) == norm:
                    for a in item.get("aliases", []):
                        na = normalize_name(a)
                        if na and na not in seen_aliases and na != norm:
                            existing["aliases"].append(a)
                            seen_aliases.add(na)
                    existing["frequency"] = item.get("frequency", 0)
                    break
            continue

        # 检查别名是否匹配已有条目
        merged = False
        for a in item.get("aliases", []):
            na = normalize_name(a)
            if na in seen:
                for existing in taxonomy["ship_types"]:
                    if normalize_name(existing["canonical_name"]) == na:
                        # 把 canonical_name 作为别名加入
                        if norm not in [normalize_name(x) for x in existing["aliases"]] and norm != normalize_name(existing["canonical_name"]):
                            existing["aliases"].append(name)
                        existing["frequency"] = item.get("frequency", 0)
                        merged = True
                        break
                break

        if merged:
            continue

        # 新条目，尝试分类
        category = guess_category(name)
        taxonomy["ship_types"].append({
            "canonical_name": name,
            "aliases": [a for a in item.get("aliases", []) if normalize_name(a) not in seen_aliases],
            "category": category,
            "parent": category,
            "size": "",
            "frequency": item.get("frequency", 0),
            "source": "news_extracted"
        })
        seen.add(norm)

    return taxonomy


def guess_category(name: str) -> str:
    """根据名称猜测分类"""
    name_lower = name.lower()
    keywords = {
        "集装箱船": ["集装箱", "container", "teu", "boxship"],
        "散货船": ["散货", "bulk", "capesize", "panamax", "handysize", "vloc", "newcastlemax"],
        "油轮": ["油轮", "油船", "tanker", "vlcc", "suezmax", "aframax", "lr1", "lr2", "mr", "product"],
        "液化气运输船": ["液化", "lng", "lpg", "乙烷", "ethane", "氨", "ammonia", "co2", "二氧化碳", "gas carrier"],
        "汽车运输船": ["汽车", "car carrier", "pcc", "pctc", "vehicle"],
        "滚装船": ["滚装", "ro-ro", "roro", "客滚", "渡轮", "ferry", "邮轮", "cruise"],
        "多用途船": ["多用途", "mpp", "mpv", "重吊", "heavy lift", "杂货"],
        "海工平台": ["fpso", "flng", "fsru", "半潜式平台", "自升式平台", "钻井平台", "钻井船", "半潜船"],
        "海工辅助船": ["海工辅助", "osv", "psv", "ahts", "ctv", "sov", "海缆", "铺管", "平台供应", "三用工作", "风电运维"],
        "特种船": ["冷藏", "牲畜", "木屑", "水泥", "挖泥", "起重", "拖轮", "救助", "破冰", "科考"],
    }
    for cat, kws in keywords.items():
        for kw in kws:
            if kw in name_lower:
                return cat
    return "其他"


def call_llm_validate(taxonomy: Dict) -> str:
    """调用 LLM 检查重复和分类问题"""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY not set")

    client = OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    # 准备检查内容：所有 canonical_name + aliases
    entries = []
    for s in taxonomy["ship_types"]:
        entries.append({
            "canonical_name": s["canonical_name"],
            "aliases": s["aliases"],
            "category": s["category"],
            "parent": s["parent"]
        })

    prompt = f"""你是一位船舶类型分类专家。请检查以下船型分类表，找出并修正以下问题：

1. **重复名称**：同一个船型出现在多个条目中（canonical_name 或 aliases 重复）
2. **层级错误**：子类型被错误地放到顶层，如 "VLCC" 应属于 "原油轮"，"LR2" 应属于 "成品油轮"
3. **分类错误**：某船型不属于当前 category
4. **遗漏别名**：常见别名未收录

注意：
- 集装箱船按 TEU 分：Small Feeder, Feeder, Feedermax, Panamax, Post-Panamax, Neo-Panamax, ULCV
- 散货船按 DWT 分：Mini Bulker, Handysize, Handymax, Supramax, Ultramax, Panamax/Kamsarmax, Post-Panamax, Capesize, Newcastlemax, VLOC/Valemax
- 原油轮按 DWT 分：ULCC, VLCC, Suezmax, Aframax, Panamax
- 成品油轮按 DWT 分：LR2, LR1, MR, Handy, Small
- 化学品船、沥青船等是独立类别

请输出修正后的完整 JSON 数组（仅输出数组，不要额外文字）：
[
  {{"canonical_name": "...", "aliases": ["..."], "category": "顶层分类", "parent": "直接父级", "size": "...", "source": "..."}},
  ...
]

当前分类表（共 {len(entries)} 条）：
{json.dumps(entries, ensure_ascii=False, indent=2)}
"""

    response = client.chat.completions.create(
        model="qwen-plus",
        messages=[
            {"role": "system", "content": "你是航运领域的船型分类专家，擅长识别船型层级关系和去重。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=8000,
    )
    return response.choices[0].message.content.strip()


def parse_llm_json(raw: str) -> List[Dict]:
    if not raw:
        return []
    text = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except Exception:
        # 尝试提取数组
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return []


def main():
    print("📥 加载从新闻提取的船型词表...")
    extracted = load_extracted_vocab()
    print(f"   提取到 {len(extracted)} 个船型")

    print("\n🏗️ 构建基础层级分类表...")
    taxonomy = build_taxonomy()
    print(f"   基础分类：{len(taxonomy['ship_types'])} 个船型")

    print("\n🔄 合并新闻提取的船型...")
    taxonomy = merge_extracted(taxonomy, extracted)
    print(f"   合并后：{len(taxonomy['ship_types'])} 个船型")

    print("\n🤖 使用 AI 验证和修正分类表...")
    raw = call_llm_validate(taxonomy)
    # 保存原始 AI 响应用于调试
    log_path = OUTPUT_TAXONOMY.with_suffix(".llm_raw.json")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(raw)
    validated = parse_llm_json(raw)
    if validated:
        taxonomy["ship_types"] = validated
        print(f"   AI 修正后：{len(taxonomy['ship_types'])} 个船型")
    else:
        print("   ⚠️ AI 返回解析失败，使用基础合并结果")
        print(f"   原始响应已保存到：{log_path}")

    # 添加统计
    taxonomy["metadata"] = {
        "total_ship_types": len(taxonomy["ship_types"]),
        "source": ["industry_standard", "news_extracted", "ai_validated"],
        "categories": list(taxonomy["taxonomy"].keys())
    }

    # 保存
    OUTPUT_TAXONOMY.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_TAXONOMY, "w", encoding="utf-8") as f:
        json.dump(taxonomy, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 已保存完整船型分类表：{OUTPUT_TAXONOMY}")
    print(f"   共 {len(taxonomy['ship_types'])} 个船型")


if __name__ == "__main__":
    main()

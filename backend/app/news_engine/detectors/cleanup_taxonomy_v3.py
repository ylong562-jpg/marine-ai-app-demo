# -*- coding: utf-8 -*-
"""
第三次清理：合并 AI 重复生成的独立 canonical 条目，修正别名冲突。
"""

import sys
import json
import re
from pathlib import Path

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
TAXONOMY_FILE = TARGET_DIR / "vocabularies" / "ship_type_taxonomy.json"

# 合并映射：源 canonical_name -> 目标 canonical_name
MERGE_MAP = {
    "VLCC": "超大型原油船",
    "VLGC": "超大型液化石油气船",
    "液化石油气船": "液化石油气运输船",
}

# 需要从指定条目的别名中删除的冲突词
ALIAS_REMOVE = {
    "起重船": ["重吊船"],
}


def normalize(name: str) -> str:
    return re.sub(r"[\s\-_/]+", "", name.strip().lower())


def main():
    with open(TAXONOMY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    by_name = {normalize(s["canonical_name"]): s for s in data["ship_types"]}

    # 合并独立条目
    for src, dst in MERGE_MAP.items():
        src_item = by_name.get(normalize(src))
        dst_item = by_name.get(normalize(dst))
        if src_item and dst_item:
            # 转移别名
            for a in src_item.get("aliases", []):
                if a not in dst_item["aliases"]:
                    dst_item["aliases"].append(a)
            # 源 canonical_name 加为别名
            if src not in dst_item["aliases"]:
                dst_item["aliases"].append(src)
            data["ship_types"].remove(src_item)

    # 删除冲突别名
    for item in data["ship_types"]:
        name = item["canonical_name"]
        if name in ALIAS_REMOVE:
            removes = set(ALIAS_REMOVE[name])
            item["aliases"] = [a for a in item["aliases"] if a not in removes]

    # 重新全局去重别名
    seen_aliases = {}
    for item in data["ship_types"]:
        norm_name = normalize(item["canonical_name"])
        unique = []
        for a in item.get("aliases", []):
            na = normalize(a)
            if not na or na == norm_name:
                continue
            if na in seen_aliases and seen_aliases[na] != item["canonical_name"]:
                continue
            seen_aliases[na] = item["canonical_name"]
            unique.append(a)
        item["aliases"] = unique

    # 更新 metadata
    data["metadata"]["total_ship_types"] = len(data["ship_types"])

    with open(TAXONOMY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ 已清理分类表：{TAXONOMY_FILE}")
    print(f"   共 {len(data['ship_types'])} 个船型")


if __name__ == "__main__":
    main()

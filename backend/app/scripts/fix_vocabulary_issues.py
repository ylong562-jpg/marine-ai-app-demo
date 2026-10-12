#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _BACKEND_DIR)
from app.api.correction_vocabulary import _sync_entity_vocabulary_to_mysql
import sqlite3

DB_PATH = os.path.join(_BACKEND_DIR, "app", "news_engine", "data", "entity_registry.db")

# 需要修正的记录：[(canonical_name, entity_type, new_english_name, action)]
# action: 'update_english' 或 'delete' 或 'rename'
CORRECTIONS = [
    # 修正英文翻译错误
    ('达门造船集团', 'shipyard', 'Damen Shipyards Group', 'update_english'),
    ('武昌造船', 'shipyard', 'Wuchang Shipbuilding', 'update_english'),
    ('韩华海洋', 'shipyard', 'Hanwha Ocean Co., Ltd.', 'update_english'),
    ('中国远洋海运集团有限公司', 'shipowner', 'China COSCO Shipping Corporation Limited', 'update_english'),
    # 删除重复/有问题的记录
    ('明阳智能能源集团', 'company', None, 'delete'),  # 与 明阳智慧能源集团股份有限公司 重复
    ('船舶海运', 'buyer', None, 'delete'),  # 翻译过于宽泛，可能是误提取
]

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
for canonical, entity_type, new_eng, action in CORRECTIONS:
    if action == 'update_english':
        cur.execute(
            "UPDATE entities SET english_name = ? WHERE canonical_name = ? AND entity_type = ?",
            (new_eng, canonical, entity_type)
        )
        print(f"[UPDATE] [{entity_type}] {canonical} -> {new_eng}")
    elif action == 'delete':
        cur.execute(
            "DELETE FROM entities WHERE canonical_name = ? AND entity_type = ?",
            (canonical, entity_type)
        )
        print(f"[DELETE] [{entity_type}] {canonical}")
conn.commit()
conn.close()

print("\n同步到 MySQL...")
_sync_entity_vocabulary_to_mysql()
print("同步完成")

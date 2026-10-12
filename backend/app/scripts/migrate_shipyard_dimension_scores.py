# -*- coding: utf-8 -*-
"""
迁移脚本：创建 shipyard_dimension_scores 表，存储船厂六维评分结果。

表结构：
    shipyard_id      船厂 ID（shipyard_data.id）
    dimension        维度中文名（接单能力/交付履约/技术实力/经营稳定性/安全合规/产能设施）
    score            维度得分 1~100
    explanation      LLM 生成的中文解读
    evidence_count   该维度计入的 evidence 条数
    evidence_summary 类别聚合分 + top3 证据（JSON）
    model_version    评分模型版本（权重文件 model_version 一致）
    computed_at      计算时间
    主键 (shipyard_id, dimension)

运行方式：
    cd <repo>/backend
    source .venv/bin/activate
    python app/scripts/migrate_shipyard_dimension_scores.py
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings

DB_CONFIG = get_settings().NEWS_DB_CONFIG
TABLE = "shipyard_dimension_scores"


def main():
    import mysql.connector

    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                shipyard_id BIGINT NOT NULL,
                dimension VARCHAR(50) NOT NULL,
                score DOUBLE NOT NULL,
                explanation TEXT,
                evidence_count INT DEFAULT 0,
                evidence_summary JSON,
                model_version VARCHAR(50) NOT NULL,
                computed_at DATETIME NOT NULL,
                PRIMARY KEY (shipyard_id, dimension),
                INDEX idx_dimension (dimension)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        conn.commit()
        print(f"[DB] 表 {TABLE} 已创建或已存在")
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()

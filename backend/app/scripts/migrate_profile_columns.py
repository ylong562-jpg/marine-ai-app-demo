"""
Migration: add shipyard profile columns to Time_Series.shipyard_data.
- employees INT
- utilization_rate DECIMAL(5,4)
- orderbook_months INT
- founded_year INT
"""
import mysql.connector

DB_CONFIG = {
    'host': 'your-db-host.example.com',
    'user': 'your-db-user',
    'password': 'your-db-password',
    'database': 'Time_Series',
    'port': 3306
}

COLUMNS = {
    'employees': "INT NULL COMMENT '员工数'",
    'utilization_rate': "DECIMAL(5,4) NULL COMMENT '产能利用率 0-1'",
    'orderbook_months': "INT NULL COMMENT '订单覆盖月数'",
    'founded_year': "INT NULL COMMENT '成立年份'",
}


def migrate():
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    try:
        cursor.execute("DESCRIBE shipyard_data")
        existing = {row[0] for row in cursor.fetchall()}
        for col, defn in COLUMNS.items():
            if col in existing:
                print(f"[SKIP] {col} 已存在")
                continue
            cursor.execute(f"ALTER TABLE shipyard_data ADD COLUMN {col} {defn}")
            print(f"[OK] {col} 已添加")
        conn.commit()
        print("[OK] 迁移完成")
    except Exception as e:
        conn.rollback()
        print(f"[ERR] {e}")
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    migrate()

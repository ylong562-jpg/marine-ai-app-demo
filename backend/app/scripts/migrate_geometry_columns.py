"""
Migration: add geometry columns to Time_Series.shipyard_data.
- lat DECIMAL(10,7)
- lng DECIMAL(11,7)
- boundary_geo JSON
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
    'lat': "DECIMAL(10,7) NULL COMMENT 'WGS-84 latitude'",
    'lng': "DECIMAL(11,7) NULL COMMENT 'WGS-84 longitude'",
    'boundary_geo': "JSON NULL COMMENT '船厂范围框四顶点 [lng,lat] 数组'",
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

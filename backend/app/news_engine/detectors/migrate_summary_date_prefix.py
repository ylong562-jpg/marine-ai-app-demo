# -*- coding: utf-8 -*-
"""
迁移脚本：为 ship_news 表中已有的 summary 字段补全年份前缀。

功能：
1. 连接数据库，读取所有新闻的 id、date、summary。
2. 在 summary 开头注入年份前缀说明（与 process_single_news 保持一致）。
3. 仅当 summary 实际发生变化时才更新数据库，避免无谓写入。

使用方法：
    python migrate_summary_date_prefix.py

注意：
    - 本脚本只更新 summary 字段，不会重新生成 entity/data/news_report。
    - 不修改正文内容，保留原文样貌以便后续处理。
    - 执行前建议先备份数据库。
"""

import sys
import os
import re

# 确保当前目录在 Python 路径中（支持 PyInstaller 打包环境）
if getattr(sys, 'frozen', False):
    current_dir = os.path.dirname(sys.executable)
    internal_dir = os.path.join(current_dir, '_internal')
    if os.path.exists(internal_dir):
        sys.path.insert(0, internal_dir)
    sys.path.insert(0, current_dir)
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from complete_news_pipeline import connect_to_database

# 年份前缀检测正则：匹配 summary 开头是否已有 "【该新闻的日期是YYYY年"
_DATE_PREFIX_RE = re.compile(r'^【该新闻的日期是\d{4}年')

from db_config import DB_CONFIG as DEFAULT_DB_CONFIG


def _build_date_prefix(news_date) -> str:
    """根据新闻日期构建年份前缀。"""
    if not news_date:
        return ''
    pub_year_match = re.search(r'(\d{4})', str(news_date))
    if not pub_year_match:
        return ''
    pub_year = pub_year_match.group(1)
    return (
        f"【该新闻的日期是{pub_year}年，"
        f"如果每笔交易的订单信息没有明确的年份信息，默认为新闻日期的年份信息。】\n\n"
    )


def _add_date_prefix(summary: str, news_date) -> str:
    """对单条 summary 注入年份前缀（不修改正文）。"""
    if not summary:
        return summary

    summary = str(summary)
    prefix = _build_date_prefix(news_date)
    if prefix and not _DATE_PREFIX_RE.match(summary):
        summary = prefix + summary

    return summary


def migrate_summary_date_prefix(db_config=None, batch_size=100):
    """
    批量迁移 ship_news 表的 summary 字段。

    Parameters:
        db_config: 数据库配置字典，默认使用 DEFAULT_DB_CONFIG
        batch_size: 每批读取的记录数
    """
    db_config = db_config or DEFAULT_DB_CONFIG
    connection = connect_to_database(**db_config)
    if not connection:
        print("❌ 数据库连接失败，迁移终止")
        return

    try:
        cursor = connection.cursor(dictionary=True)

        # 获取总记录数
        cursor.execute("SELECT COUNT(*) AS total FROM ship_news")
        total = cursor.fetchone()['total']
        print(f"📊 ship_news 表共有 {total} 条记录")

        updated_count = 0
        skipped_count = 0
        error_count = 0
        offset = 0

        while offset < total:
            cursor.execute(
                "SELECT id, date, summary FROM ship_news ORDER BY id LIMIT %s OFFSET %s",
                (batch_size, offset)
            )
            rows = cursor.fetchall()
            if not rows:
                break

            update_values = []
            for row in rows:
                try:
                    original_summary = row.get('summary') or ''
                    prefixed_summary = _add_date_prefix(original_summary, row.get('date'))

                    if prefixed_summary == original_summary:
                        skipped_count += 1
                        continue

                    update_values.append((prefixed_summary, row['id']))
                except Exception as e:
                    print(f"   ⚠️ 处理 id={row.get('id')} 时异常: {e}")
                    error_count += 1

            # 批量更新
            if update_values:
                update_sql = "UPDATE ship_news SET summary = %s WHERE id = %s"
                cursor.executemany(update_sql, update_values)
                connection.commit()
                updated_count += len(update_values)
                print(f"   ✅ 已更新 {len(update_values)} 条 (累计 {updated_count})")

            offset += batch_size

        print("\n" + "=" * 60)
        print("📊 迁移完成统计:")
        print(f"   总记录数: {total}")
        print(f"   已更新:   {updated_count}")
        print(f"   无变化:   {skipped_count}")
        print(f"   异常:     {error_count}")
        print("=" * 60)

    except Exception as e:
        print(f"❌ 迁移过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if connection:
            connection.close()
            print("🔌 数据库连接已关闭")


if __name__ == "__main__":
    migrate_summary_date_prefix()

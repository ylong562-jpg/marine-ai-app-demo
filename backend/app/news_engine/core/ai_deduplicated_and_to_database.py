"""
AI去重和数据库导入模块 - 完整版
调用完整的新闻处理流程，确保所有字段都被正确生成

⚠️ 注意：此文件作为 complete_news_pipeline.py 的替代入口
建议直接运行 complete_news_pipeline.py 获取完整功能
"""

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

import asyncio
import mysql.connector

# 导入完整流程
from complete_news_pipeline import (
    run_complete_pipeline,
    connect_to_database,
    ensure_table_schema,
    import_news_to_database,
    deduplicate_with_database
)
from ai_classify import re_classify
from ai_news_core_content_extraction import process_news_with_category_prompts
from ai_news_report_creating import generate_news_report_from_existing_data
from news_combine import get_combined_news_with_metadata
from ai_deduplicate_engine import ai_deduplicate_news


async def fetch_existing_news_by_category(connection, days_back, categories):
    """
    保留此函数以兼容旧代码
    """
    from complete_news_pipeline import fetch_existing_news_by_category as fetch_func
    return await fetch_func(connection, days_back, categories)


async def deduplicate_single_category(analyzer, new_news_list, existing_news_list, threshold):
    """
    保留此函数以兼容旧代码
    """
    deduplicated = []
    
    for new_news in new_news_list:
        is_duplicate = False
        new_text = new_news.get('news_report', '') or new_news.get('summary', '')
        
        if new_text:
            for existing_news in existing_news_list:
                existing_text = existing_news.get('news_report', '') or existing_news.get('summary', '')
                
                if new_text and existing_text:
                    from complete_news_pipeline import check_similarity
                    similarity = await check_similarity(
                        analyzer, new_text, existing_text,
                        new_news['title'], existing_news['title']
                    )
                    
                    if similarity >= threshold:
                        is_duplicate = True
                        break
        
        if not is_duplicate:
            deduplicated.append(new_news)
    
    return deduplicated


async def check_news_similarity(analyzer, text1, text2, title1="", title2=""):
    """
    保留此函数以兼容旧代码
    """
    from complete_news_pipeline import check_similarity
    return await check_similarity(analyzer, text1, text2, title1, title2)


# 主处理函数 - 独立的完整流程（与 complete_news_pipeline.py 保持一致）
async def main_processed():
    """
    主函数 - 完整的新闻处理流程（包含重新分类）
    
    完整流程：
    1. 获取新闻（带source_platform和confidence_score）
    2. AI前置去重
    3. AI重新分类（8个核心主维度）
    4. 内容提取（根据新分类使用不同prompts）
    5. 生成新闻报告（news_report）
    6. 数据库去重
    7. 导入数据库
    """
    
    print("\n" + "="*70)
    print("🚀 启动AI新闻处理流程（ai_deduplicated_and_to_database）")
    print("="*70)
    
    # 数据库配置
    from db_config import DB_CONFIG as db_config
    
    try:
        # 步骤1: 获取新闻数据
        print("\n📥 步骤1: 获取新闻数据...")
        news_data = await get_combined_news_with_metadata()
        if not news_data:
            print("❌ 没有获取到新闻数据")
            return []
        print(f"✅ 获取到 {len(news_data)} 条新闻")
        
        # 步骤2: AI前置去重
        print("\n🔍 步骤2: AI前置去重...")
        news_data = await ai_deduplicate_news(news_data, use_grouping=True)
        print(f"✅ 去重后剩余 {len(news_data)} 条新闻")
        
        # 步骤3: AI重新分类（关键步骤）
        print("\n🏷️ 步骤3: AI重新分类...")
        news_data = await re_classify(news_data=news_data)
        if not news_data:
            print("❌ 重新分类失败")
            return []
        print(f"✅ 重新分类完成，共 {len(news_data)} 条")
        
        # 步骤4: 内容提取（根据新分类使用不同prompts）
        print("\n📝 步骤4: 提取核心内容...")
        news_data = await process_news_with_category_prompts(news_data)
        news_data = [n for n in news_data if n is not None]  # 过滤失败项
        print(f"✅ 内容提取完成，共 {len(news_data)} 条有效新闻")
        
        # 步骤5: 生成新闻报告
        print("\n📰 步骤5: 生成新闻报告...")
        news_data = await generate_news_report_from_existing_data(news_data)
        print(f"✅ 报告生成完成")
        
        # 步骤6-7: 数据库操作
        print("\n💾 步骤6: 连接数据库...")
        connection = connect_to_database(**db_config)
        if not connection:
            print("❌ 数据库连接失败")
            return news_data
        
        await ensure_table_schema(connection)
        
        print("\n🗄️ 步骤7: 与数据库历史数据去重...")
        news_data = await deduplicate_with_database(connection, news_data)
        print(f"✅ 去重后剩余 {len(news_data)} 条")
        
        if news_data:
            print("\n📤 步骤8: 导入数据库...")
            await import_news_to_database(connection, news_data)
        
        connection.close()
        
        print("\n" + "="*70)
        print(f"✅ 流程完成！共处理 {len(news_data)} 条新闻")
        print("="*70)
        
        # 显示样例
        if news_data:
            print("\n导入数据样例:")
            for i, news in enumerate(news_data[:3], 1):
                print(f"\n{i}. {news.get('title', 'N/A')[:50]}...")
                print(f"   分类: {news.get('category', 'N/A')}")
                print(f"   主标签: {news.get('main_tags', 'N/A')}")
                print(f"   来源: {news.get('source_platform', 'N/A')}")
                print(f"   置信度: {news.get('confidence_score', 'N/A')}%")
                print(f"   实体: {news.get('entity', 'N/A')}")
                print(f"   标签: {news.get('tags', 'N/A')}")
        
        return news_data
        
    except Exception as e:
        print(f"❌ 处理失败: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    asyncio.run(main_processed())

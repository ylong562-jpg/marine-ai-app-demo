#!/bin/bash
# 两个后台长任务的进度速查：进程状态 + 日志统计 + DB 落库计数。
# 任何会话（含全新上下文）都可直接运行本脚本恢复现场。
BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BACKEND"
PY=.venv/bin/python

echo "========== 1. 行业新闻回填 =========="
if pgrep -f "reprocess_database_news.py --all --categories 行业新闻" > /dev/null; then
    echo "状态: 运行中 (PID $(cat output/backfill.pid 2>/dev/null || pgrep -f 'reprocess_database_news' | head -1))"
else
    echo "状态: 未运行"
fi
L=$(ls -t output/backfill_industry_2026*.log 2>/dev/null | head -1)
if [ -n "$L" ]; then
    echo "日志: $L"
    echo "  本次成功: $(grep -c '✅ 新闻处理完成' "$L" 2>/dev/null)  失败: $(grep -c '🗑️ 删除处理失败' "$L" 2>/dev/null)  Arrearage: $(grep -c 'Arrearage' "$L" 2>/dev/null)"
    grep -E "🧩 第|累计已更新" "$L" 2>/dev/null | tail -2 | sed 's/^/  /'
fi

echo "========== 2. 船厂维度计算 =========="
if pgrep -f "app.shipyard_dimensions --all" > /dev/null; then
    echo "状态: 运行中 (PID $(cat output/dimensions.pid 2>/dev/null || pgrep -f 'app.shipyard_dimensions' | head -1))"
else
    echo "状态: 未运行"
fi
L=$(ls -t output/dimensions_full_*.log 2>/dev/null | head -1)
if [ -n "$L" ]; then
    echo "日志: $L"
    echo "  写入船厂: $(grep -c '已写入' "$L" 2>/dev/null)  失败: $(grep -c '❌' "$L" 2>/dev/null)"
    tail -2 "$L" 2>/dev/null | sed 's/^/  /'
fi

echo "========== 3. 数据库落库 =========="
$PY - <<'EOF'
import mysql.connector
from app.core.config import Settings
s = Settings()
conn = mysql.connector.connect(**s.NEWS_DB_CONFIG)
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM ship_news WHERE JSON_CONTAINS(NULLIF(tags,''), %s, '$.main_tags') AND INSTR(data,'shipyard_related')>0", ('"行业新闻"',))
with_field = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM ship_news WHERE JSON_CONTAINS(NULLIF(tags,''), %s, '$.main_tags') AND INSTR(data,'shipyard_related')=0 AND INSTR(data,'summary_type')=0", ('"行业新闻"',))
truly_pending = cur.fetchone()[0]
print(f"行业新闻新格式: {with_field} / 6364；真正未完成: {truly_pending} 条；订单汇总独立schema(正常): {6364 - with_field - truly_pending} 条")
cur.execute("SELECT COUNT(DISTINCT shipyard_id) FROM shipyard_dimension_scores")
print(f"维度已覆盖船厂: {cur.fetchone()[0]} / 227")
cur.execute("SELECT COUNT(*) FROM pmc_builder_map")
print(f"builder映射条目: {cur.fetchone()[0]}")
conn.close()
EOF

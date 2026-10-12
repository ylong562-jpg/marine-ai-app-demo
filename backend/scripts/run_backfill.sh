#!/bin/bash
# 行业新闻 core_content 回填——离线断点续跑包装脚本。
# 已在运行则直接退出（幂等）；否则按固定参数启动并记录 PID 与日志路径。
# 续跑原理：--require-data-field shipyard_related 只取未处理行 + 每块独立提交。
set -e
BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BACKEND"

if pgrep -f "reprocess_database_news.py --all --categories 行业新闻" > /dev/null; then
    echo "回填已在运行 (PID $(pgrep -f 'reprocess_database_news.py --all --categories 行业新闻' | head -1))，不重复启动"
    exit 0
fi

set -a; . ./service.env 2>/dev/null || true; set +a
mkdir -p output
LOG="output/backfill_industry_$(date +%Y%m%d_%H%M%S).log"
cd app/news_engine
nohup ../../.venv/bin/python reprocess_database_news.py \
    --all --categories 行业新闻 --force --steps core_content \
    --chunk-size 300 --require-data-field shipyard_related --skip-history-dedup \
    > "$BACKEND/$LOG" 2>&1 &
PID=$!
echo "$PID" > "$BACKEND/output/backfill.pid"
echo "回填已启动 PID=$PID 日志=$LOG"

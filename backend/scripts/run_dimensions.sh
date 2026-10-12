#!/bin/bash
# 船厂六维评分全量计算（NNLS 拟合 + 出分）——离线断点续跑包装脚本。
# 已在运行则直接退出（幂等）；--skip-computed 跳过当前版本六维已齐的船厂。
set -e
BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BACKEND"

if pgrep -f "app.shipyard_dimensions --all" > /dev/null; then
    echo "维度计算已在运行 (PID $(pgrep -f 'app.shipyard_dimensions --all' | head -1))，不重复启动"
    exit 0
fi

set -a; . ./service.env 2>/dev/null || true; set +a
mkdir -p output
LOG="output/dimensions_full_$(date +%Y%m%d_%H%M%S).log"
nohup .venv/bin/python -m app.shipyard_dimensions \
    --all --fit-weights --news-limit 200 --skip-computed \
    > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$BACKEND/output/dimensions.pid"
echo "维度计算已启动 PID=$PID 日志=$LOG"

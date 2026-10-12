#!/bin/bash
# Marine AI 新闻定时任务脚本（AI 再分析模式）
# 每天 6:00 执行一次，仅对已有新闻进行 AI 分类、核心内容提取和报告生成。
# 注意：本脚本不再执行任何外部网站/公众号爬取；新数据由其他服务器写入 ship_news.summary。
# cron: 0 6 * * * <repo>/backend/app/scripts/run_news_crawler.sh >> /var/log/marine-ai-news-crawler.log 2>&1

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../" && pwd)"
LOG_FILE="/var/log/marine-ai-news-crawler.log"

# 确保日志文件存在并可写
touch "$LOG_FILE" 2>/dev/null || true

echo "=========================================="
echo "🚀 启动 Marine AI 新闻 AI 再分析任务"
echo "⏰ 执行时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

cd "$PROJECT_DIR"

# 激活项目虚拟环境
if [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    source "$PROJECT_DIR/.venv/bin/activate"
else
    echo "❌ 未找到虚拟环境: $PROJECT_DIR/.venv"
    exit 1
fi

# 固定 Chrome/Chromedriver 路径，避免 Selenium Manager 找到版本不匹配的浏览器
if [ -x "/opt/google/chrome/chrome" ] && [ -x "/opt/chromedriver-linux64/chromedriver" ]; then
    export CHROME_BIN="/opt/google/chrome/chrome"
    export CHROMEDRIVER_PATH="/opt/chromedriver-linux64/chromedriver"
    # 把正确的 chromedriver 目录放到 PATH 最前，兜底 Selenium Manager
    export PATH="/opt/chromedriver-linux64:$PATH"
fi

# 注入 Python 路径，确保脚本能找到项目模块
# news_engine 必须放在最前，避免 app/core 等同名包覆盖
export PYTHONPATH="$PROJECT_DIR/app/news_engine:$PROJECT_DIR:$PROJECT_DIR/app${PYTHONPATH:+:$PYTHONPATH}"

# 加载环境变量（service.env 包含 DASHSCOPE_API_KEY 等）
if [ -f "$PROJECT_DIR/service.env" ]; then
    set -a
    source "$PROJECT_DIR/service.env"
    set +a
fi

# 仅执行数据库内已有新闻的 AI 再处理：
#   reclassify   -> 根据 summary 重新分类
#   core_content -> 提取核心内容
#   news_report  -> 生成报告
# 默认处理最近 2 天的记录；可通过脚本参数覆盖，例如：
#   run_news_crawler.sh 1 5    （最近 1 天，最多 5 条）
DAYS="${1:-2}"
LIMIT_ARG=()
if [ $# -ge 2 ]; then
    LIMIT_ARG=("$2")
fi
python "$PROJECT_DIR/app/scripts/run_refresh_body.py" "$DAYS" --steps reclassify,core_content,news_report "${LIMIT_ARG[@]}"

# 兜底清理：如果未来脚本重新启用浏览器爬虫，防止遗留 Chrome/Chromedriver 进程
if [ -x /usr/local/bin/cleanup_chrome_orphans.sh ]; then
    /usr/local/bin/cleanup_chrome_orphans.sh >/dev/null 2>&1 || true
fi

echo "✅ AI 再分析任务完成: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

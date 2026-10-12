# Marine Intelligence — 海事智能平台（后端）

面向航运/造船行业的智能数据平台，基于大语言模型（LLM）提供新闻情报、船厂数据库、市场数据采集与项目任务进展跟踪四大业务能力，支持多用户、注册审批与角色权限（管理员 / 任务管理员）。本项目为该平台的**后端服务**（FastAPI），对外提供完整的 REST API。

> **关于前端**：本平台的前端界面因涉及公司内部业务信息（客户项目数据、内部操作流程与界面设计），出于保密原因**不在本仓库中公开**。本仓库保留全部后端源码与接口文档，前端可通过本文档中的 API 独立对接。

| 板块 | 能力 |
|------|------|
| 📰 新闻情报 | 多源航运新闻爬取 → AI 分类/去重/实体归一 → 每日报告与网页生成 → 人工矫正与词表管理 → 热点聚焦 |
| 🏗️ 船厂数据库 | 船厂档案管理（订单/建造/新闻聚合）、AI 从文本/文件提取结构化信息、六维能力评分（接单/交付/技术/经营/合规/产能） |
| 📊 数据采集 | 航运指数图表与周报、新造船/买卖船/租船市场分析、利率数据导入 |
| ✅ 项目任务进展 | 项目主数据同步（飞书多维表格）、关联邮箱邮件 AI 摘要与进展总结、待办任务管理 |

## 技术架构

```
客户端（浏览器 / 第三方系统）
      │ HTTP / SSE
FastAPI (backend/app, Port 8000)
  ├─ api/            15 个路由模块（见下方接口说明）
  ├─ services/       业务层 + 2 个后台 watcher（飞书同步、邮件扫描）
  ├─ news_engine/    新闻 AI 全流程引擎（可 CLI 独立运行）
  ├─ shipyard_ai/    船厂信息 LLM 提取
  └─ shipyard_dimensions/  六维评分（LLM 打分 + NNLS 权重拟合）
      │
      ├─ SQLite (async)     用户/项目/任务/邮件摘要/审计日志（backend/data/app.db）
      ├─ MySQL Time_Series  新闻库 ship_news、市场分析、指数、评分
      └─ 外部 API           DashScope LLM、飞书开放平台、Esri 卫星影像
```

**技术栈**：FastAPI + SQLAlchemy 2.0 (async) + JWT ｜ SQLite（本地）+ MySQL（业务）｜ DashScope（OpenAI 兼容接口）｜ crawl4ai / Playwright / Selenium（爬虫）｜ Docker

## 目录结构

```
marine-ai-app-demo/
├── backend/
│   ├── app/
│   │   ├── main.py                  # 入口：CORS、路由挂载、启动迁移、后台 watcher
│   │   ├── api/                     # 15 个路由模块（见接口说明）
│   │   ├── core/                    # config.py（全部环境变量）、security.py（JWT）
│   │   ├── db/                      # 异步引擎 + ORM 模型
│   │   ├── models/schemas.py        # Pydantic 请求/响应模型
│   │   ├── services/                # 业务层（auth/邮件 watcher/飞书/市场分析/指数报告…）
│   │   ├── news_engine/             # 新闻爬取 + AI pipeline（CLI 可独立运行）
│   │   ├── shipyard_ai/             # 船厂信息 AI 提取
│   │   ├── shipyard_dimensions/     # 六维评分（data/weights.json 为拟合权重）
│   │   └── scripts/                 # 数据迁移/修复/离线批处理脚本
│   ├── scripts/                     # 运维脚本（run_backfill / run_dimensions / task_status）
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── service.env.example          # 服务凭据样例
│   └── start_uvicorn.sh
├── docker-compose.yml               # 后端一键编排
└── .env.example                     # LLM 配置样例
```

## 接口说明

统一前缀 `/api/v1`，认证方式 `Authorization: Bearer <JWT>`（登录/注册除外）；标注「管理员」的接口需 `is_admin`，任务相关接口需 `is_project_manager` 或管理员。

### 认证 `auth`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/register` | 用户注册（受 `ALLOW_REGISTRATION` 控制，需管理员审批后登录） |
| POST | `/auth/login` | 登录，返回 JWT |
| GET | `/auth/me` | 当前用户信息（含角色标识） |

### 用户管理 `admin`（管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/users` / `/admin/users/pending` | 全部用户 / 待审核用户 |
| POST | `/admin/users/{id}/approve` / `reject` | 通过 / 拒绝注册 |
| POST | `/admin/users/{id}/promote` / `demote` | 管理员升降级 |
| POST | `/admin/users/{id}/grant-project-manager` / `revoke-project-manager` | 任务管理员授权 |
| DELETE | `/admin/users/{id}` | 删除用户 |

### 新闻情报 `news`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/news` | 新闻分页列表（平台/栏目/日期筛选） |
| PUT | `/news/{news_id}` | 编辑单条新闻 |
| GET | `/news/platforms` / `/news/columns` | 平台 / 栏目下拉选项 |
| POST | `/news/generate-webpage` | 生成新闻网页/文档 |
| POST | `/news/refresh-body` / `/news/refresh-body-single` | AI 刷新正文（批量 / 单条） |
| POST | `/news/refresh-report-preview` + `GET …/{task_id}`(+`/events`) | 刷新报告预览任务（SSE 进度） |
| POST | `/news/reprocess-preview` + `GET …/{task_id}`(+`/events`) | 重处理预览任务（SSE 进度） |
| POST | `/news/crawl-preview/start` + `GET …/{task_id}` | 爬虫抓取预览 |
| GET | `/news/analyze-url-platform` · POST `/news/analyze/start` + `GET …/{task_id}` | AI 分析导入 |
| GET | `/news/source-platforms` | 可选来源平台 |
| POST | `/news/import-selected` / `/news/batch-update` / `/news/batch-delete` | 导入 / 批量编辑 / 批量删除 |

### 重处理 `reprocess`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/reprocess/news` | 按筛选条件重新处理存量新闻 |
| POST | `/reprocess/extract/start` + `GET …/{task_id}` | 结构化提取任务 |
| POST | `/reprocess/import` | 导入重处理结果（船厂等） |

### 矫正与词表 `correction` / `correction/vocabulary`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/correction/pending` | 待人工核验新闻列表 |
| POST | `/correction/verify` / `/correction/skip` | 核验通过入库 / 跳过 |
| GET/POST | `/correction/vocabulary/entity` | 实体词表查询/维护 |
| POST | `/correction/vocabulary/entity/ai-group`（+`/async`、`GET …/status/{job_id}`） | AI 实体分组（同步/异步） |

### 船厂 `shipyards` / `shipyard-ai` / `shipyard-dimensions`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/shipyards` | 船厂档案列表 |
| GET | `/shipyards/raw` | 原始记录（导入用） |
| GET | `/shipyards/{id}` | 单条详情 |
| POST | `/shipyards/import` | 批量导入（AI 辅助字段匹配） |
| POST | `/shipyards/export` | 导出 Excel / Word |
| POST | `/shipyards/update` | 触发数据更新 |
| GET/PUT | `/shipyards/map-config` | 地图配置 |
| POST | `/shipyards/{id}/geometry` · POST `/shipyards/coordinates` | 几何 / 坐标更新 |
| POST | `/shipyards/check-name` | 重名检查 |
| DELETE | `/shipyards/{id}` | 删除 |
| GET | `/shipyards/{id}/history` · DELETE `/shipyards/history/{history_id}` | 变更历史 |
| GET | `/shipyards/{id}/orders` / `/build` / `/news` | 聚合该船厂订单/建造/新闻 |
| GET | `/shipyards/imagery-date` | Esri 卫星影像拍摄日期（7 天缓存） |
| POST | `/shipyard-ai/extract-text` / `/shipyard-ai/extract-file` | AI 从文本/文件提取船厂结构化信息 |
| GET | `/shipyard-dimensions/{shipyard_id}` | 六维评分查询 |
| POST | `/shipyard-dimensions/recompute` | 触发重算 |

### 船厂交易 `shipyard-transactions`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/shipyard-transactions/sync` · GET `/shipyard-transactions/status` | 交易数据同步（任务式） |

### 数据采集 `shipping-index` / `market-analysis` / `data-import` / `hot-focus`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/shipping-index/columns` | 指数字段 |
| POST | `/shipping-index/data` | 指数数据查询 |
| POST | `/shipping-index/update` | 更新指数数据 |
| POST | `/shipping-index/generate-latest-image` | 最新数据卡片图（PNG） |
| POST | `/shipping-index/generate-weekly-report` | 周报生成 |
| POST | `/market-analysis/records` / `/market-analysis/summary` | 交易记录 / 汇总（新造船/买卖船/租船） |
| PUT | `/market-analysis/records/{record_id}` | 单条记录修正 |
| POST | `/market-analysis/sync` + `GET …/{task_id}` | 从新闻库异步同步交易数据 |
| GET | `/market-analysis/last-sync` / `/market-analysis/ship-types` | 最近同步时间 / 船型词表 |
| POST | `/data-import/interest-rates` | 利率原文解析导入 MySQL `daily_data`（管理员，支持 dry_run） |
| GET | `/hot-focus/last-week` · POST `/hot-focus` | 热点聚焦生成 |

### 项目任务进展 `task-progress`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/task-progress/projects` | 项目列表 |
| PUT | `/task-progress/projects/{pmp_no}/track` | 开启/关闭跟踪 |
| GET/POST/DELETE | `/task-progress/projects/{pmp_no}/mailboxes` | 项目关联邮箱 CRUD |
| GET | `/task-progress/progress` / `/projects/{pmp_no}/progress` | 全部 / 单项目 AI 进展 |
| POST | `/task-progress/mail-sync/run` · GET `/task-progress/mail-sync/status` | 手动触发邮件同步 / 状态 |
| GET | `/task-progress/bitable-sync/status` | 飞书同步状态 |
| GET | `/task-progress/users` | 可分配用户 |
| GET/POST | `/task-progress/tasks` · PUT/DELETE `/tasks/{task_id}` | 待办任务 CRUD |
| GET | `/task-progress/tasks/{task_id}/logs` | 任务审计日志 |

> SSE 进度推送用于新闻情报模块的异步任务：`/news/reprocess-preview/{task_id}/events`、`/news/refresh-report-preview/{task_id}/events`（见上文新闻部分）。

### 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |

## 需要配置的数据位置

### 后端 `backend/.env`（应用配置）

| 变量 | 说明 |
|------|------|
| `JWT_SECRET_KEY` / `JWT_EXPIRE_MINUTES` | JWT 签名密钥（**生产必改**）/ 有效期（默认 14400 分钟） |
| `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD` | 首次启动时自动创建的默认管理员账号（通过环境变量设置，**生产必改**） |
| `ALLOW_REGISTRATION` | 是否开放注册（注册需管理员审批） |
| `ALLOWED_ORIGINS` | CORS 允许源（逗号分隔） |
| `DATABASE_URL` | 本地库（默认 SQLite `backend/data/app.db`，可换 PostgreSQL 等） |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | 业务 MySQL 库（默认库名 `Time_Series`） |
| `NEWS_TABLE` / `VERIFIED_NEWS_TABLE` | 新闻表名（默认 `ship_news` / `verified_news`） |

### 后端 `backend/service.env`（服务凭据，复制自 service.env.example）

| 变量 | 说明 |
|------|------|
| `DASHSCOPE_API_KEY` | LLM API Key（DashScope） |
| `CHROME_BIN` / `CHROMEDRIVER_PATH` | 爬虫浏览器路径（Selenium 源需要） |
| `TASK_MAIL_MAX_PER_CYCLE` | 邮件 watcher 每轮最大处理数（默认 3000） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭据（可选，未配置则 watcher 空闲） |
| `FEISHU_BITABLE_APP_TOKEN` / `FEISHU_BITABLE_TABLE_ID` | 飞书多维表格定位（可选） |
| `FEISHU_BITABLE_SYNC_SECONDS` | 飞书同步轮询间隔（默认 60s） |

> LLM 调用统一走 OpenAI 兼容接口，主要调用点在 `news_engine/ai_engine.py`、`shipyard_dimensions/llm.py`、`services/task_mail_service.py`，更换服务商时替换这些位置的 base_url / api_key / model 即可。

## 运行

```bash
# 本地运行
cd backend
pip install -r requirements.txt
playwright install chromium          # 需要爬虫/截图功能时
cp service.env.example service.env   # 填入 DASHSCOPE_API_KEY 等
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Docker
cp .env.example .env && docker compose up -d --build

# 新闻引擎 CLI（独立运行，无需启动 Web 服务）
cd backend && python -m app.news_engine.main
```

交互式 API 文档：启动后访问 `http://localhost:8000/docs`（Swagger UI）。

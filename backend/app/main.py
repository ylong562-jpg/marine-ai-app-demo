"""
FastAPI application entrypoint.
"""
import os

from dotenv import load_dotenv

# Load .env from project root (two levels up from app/main.py)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.core.config import get_settings
from app.api import auth, admin, news, reprocess, correction, correction_vocabulary, shipyards, shipyard_ai, shipping_index, shipyard_transactions, market_analysis, data_import, hot_focus, task_progress, shipyard_dimensions
from app.db.database import engine, Base, async_session
from app.db.models import User
from app.services.auth_service import hash_password
from app.services import task_mail_service, feishu_bitable_service

settings = get_settings()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        debug=settings.DEBUG,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
    app.include_router(admin.router, prefix=settings.API_V1_PREFIX)
    app.include_router(news.router, prefix=settings.API_V1_PREFIX)
    app.include_router(reprocess.router, prefix=settings.API_V1_PREFIX)
    app.include_router(correction.router, prefix=settings.API_V1_PREFIX)
    app.include_router(correction_vocabulary.router, prefix=f"{settings.API_V1_PREFIX}/correction")
    app.include_router(shipyards.router, prefix=settings.API_V1_PREFIX)
    app.include_router(shipyard_ai.router, prefix=settings.API_V1_PREFIX)
    app.include_router(shipping_index.router, prefix=settings.API_V1_PREFIX)
    app.include_router(shipyard_transactions.router, prefix=settings.API_V1_PREFIX)
    app.include_router(market_analysis.router, prefix=settings.API_V1_PREFIX)
    app.include_router(data_import.router, prefix=settings.API_V1_PREFIX)
    app.include_router(hot_focus.router, prefix=settings.API_V1_PREFIX)
    app.include_router(task_progress.router, prefix=settings.API_V1_PREFIX)
    app.include_router(shipyard_dimensions.router, prefix=settings.API_V1_PREFIX)

    @app.get("/health")
    async def health():
        return {"status": "ok", "app": settings.APP_NAME}

    async def _migrate_task_progress_schema():
        """旧版空表结构变更 -> 直接重建（须在 create_all 之前执行）。"""
        async with engine.begin() as conn:
            def _migrate(sync_conn):
                for tbl in ("mail_progress", "project_mailboxes"):
                    rows = sync_conn.exec_driver_sql(f"PRAGMA table_info({tbl})").fetchall()
                    if not rows:
                        continue
                    cols = {r[1] for r in rows}
                    legacy = (tbl == "mail_progress" and "ai_progress" in cols) or \
                             (tbl == "project_mailboxes" and "project_name" in cols)
                    if legacy:
                        sync_conn.exec_driver_sql(f"DROP TABLE {tbl}")
            await conn.run_sync(_migrate)

    async def _ensure_user_columns():
        """为已存在的 SQLite 库补充新增列（create_all 不会为旧表加列）。"""
        async with engine.begin() as conn:
            def _migrate(sync_conn):
                rows = sync_conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
                columns = {r[1] for r in rows}
                if "is_project_manager" not in columns:
                    sync_conn.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN is_project_manager BOOLEAN DEFAULT 0"
                    )
            await conn.run_sync(_migrate)

    async def _ensure_projects_columns():
        """projects 表补充多维表格同步所需列。"""
        async with engine.begin() as conn:
            def _migrate(sync_conn):
                rows = sync_conn.exec_driver_sql("PRAGMA table_info(projects)").fetchall()
                columns = {r[1] for r in rows}
                if "extra_fields" not in columns:
                    sync_conn.exec_driver_sql("ALTER TABLE projects ADD COLUMN extra_fields JSON")
            await conn.run_sync(_migrate)

    async def _ensure_pm_backfill():
        """权限拆分迁移：既有管理员补「任务管理员」标志，保持其原有任务能力不丢失。

        拆分后新授予的管理员不再自动获得任务权限（is_admin 与 is_project_manager 解耦），
        是否可发布/管理任务仅取决于 is_project_manager。
        """
        async with engine.begin() as conn:
            def _migrate(sync_conn):
                sync_conn.exec_driver_sql(
                    "UPDATE users SET is_project_manager = 1 WHERE is_admin = 1 AND is_project_manager = 0"
                )
            await conn.run_sync(_migrate)

    @app.on_event("startup")
    async def on_startup():
        await _migrate_task_progress_schema()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _ensure_user_columns()
        await _ensure_projects_columns()
        await _ensure_pm_backfill()

        # 启动任务进展邮件轮询（复用 feishu/email 项目的 Graph 拉取能力）
        try:
            task_mail_service.start_watcher()
        except Exception as e:
            print(f"任务进展邮件轮询启动失败: {e}")

        # 启动飞书多维表格 -> 项目主数据 轮询同步（未配置凭据时保持空闲）
        try:
            feishu_bitable_service.start_watcher()
        except Exception as e:
            print(f"多维表格同步启动失败: {e}")

        # 灌入项目主数据（幂等，已存在则跳过）
        try:
            async with async_session() as session:
                await task_mail_service.seed_projects(session)
        except Exception as e:
            print(f"项目种子数据初始化失败: {e}")

        # 一次性清除早期自动导入的不可靠关联邮箱（改为手动维护）
        try:
            await task_mail_service.purge_auto_imported_mailboxes()
        except Exception as e:
            print(f"关联邮箱清理失败: {e}")

        # Seed a default admin user if the users table is empty
        default_username = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
        default_password = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")
        async with async_session() as session:
            result = await session.execute(select(User).limit(1))
            if result.scalar_one_or_none() is None:
                admin = User(
                    username=default_username,
                    hashed_password=hash_password(default_password),
                    plain_password=default_password,
                    is_active=True,
                    is_approved=True,
                    is_admin=True,
                )
                session.add(admin)
                await session.commit()

    @app.on_event("shutdown")
    async def on_shutdown():
        await task_mail_service.stop_watcher()
        await feishu_bitable_service.stop_watcher()

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

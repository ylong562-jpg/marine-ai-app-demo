"""
飞书多维表格 -> 项目主数据（projects 表）实时同步服务。

每 FEISHU_BITABLE_SYNC_SECONDS（默认 60s）轮询一次多维表格全量记录，按 PMP号 upsert：
- 已知名称列映射到模型字段（PMP号/项目名称/阶段/优先级/类型/船东/船厂/负责人/跟进人）；
- 表中其余非空列原样存入 projects.extra_fields(JSON)，便于按需提升为正式字段；
- 只新增/更新业务字段，绝不改动 tracked（本地维护）与项目关联邮箱。

凭据未配置时同步器保持空闲并在日志中告警（不影响其他功能）。需要：
  FEISHU_APP_ID / FEISHU_APP_SECRET   飞书自建应用凭证（service.env）
  FEISHU_BITABLE_APP_TOKEN            多维表格 app_token（URL /base/ 后第一段）
  FEISHU_BITABLE_TABLE_ID             数据表 table_id（URL table= 参数）
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.db.database import async_session
from app.db.models import Project

log = logging.getLogger("bitable_sync")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
BITABLE_APP_TOKEN = os.getenv("FEISHU_BITABLE_APP_TOKEN", "HsWjbdKneaRrDssgKirclqZynMe")
BITABLE_TABLE_ID = os.getenv("FEISHU_BITABLE_TABLE_ID", "tbl91DVrWbOdJiUn")
SYNC_SECONDS = int(os.getenv("FEISHU_BITABLE_SYNC_SECONDS", "60"))
FIRST_DELAY_SECONDS = int(os.getenv("FEISHU_BITABLE_FIRST_DELAY_SECONDS", "10"))

OPEN_API_BASE = "https://open.feishu.cn/open-apis"

# 表格列名 -> Project 字段（键为去空白、小写后的列名，中英文/别名均登记）
FIELD_MAP: dict[str, str] = {
    "pmp号": "pmp_no",
    "pmp编号": "pmp_no",
    "pmp": "pmp_no",
    "project_id": "pmp_no",
    "项目名称": "name",
    "project_name": "name",
    "projectname": "name",
    "阶段": "stage",
    "stage": "stage",
    "优先级": "priority",
    "priority": "priority",
    "类型": "project_type",
    "project_type": "project_type",
    "船东/买方": "shipowner",
    "船东": "shipowner",
    "买方": "shipowner",
    "shipowner/buyer": "shipowner",
    "shipowner": "shipowner",
    "船厂/卖方": "shipyard",
    "船厂": "shipyard",
    "卖方": "shipyard",
    "shipyard/seller/maker": "shipyard",
    "shipyard/seller": "shipyard",
    "shipyard": "shipyard",
    "负责人": "manager",
    "project_manager": "manager",
    "manager": "manager",
    "跟进人": "follow_up",
    "assistant": "follow_up",
    "follow_up": "follow_up",
}

SYNC_ATTRS = ("name", "stage", "priority", "project_type", "shipowner", "shipyard", "manager", "follow_up")

_last: dict = {"running": False, "last_run_at": None, "last_result": None, "last_error": None}
_watcher_task: asyncio.Task | None = None
_warned_unconfigured = False

_token_cache: dict = {"token": None, "expire_at": 0.0}


def _configured() -> bool:
    return bool(FEISHU_APP_ID and FEISHU_APP_SECRET)


def _cell_to_str(value) -> str:
    """多维表格单元格取值统一转字符串（文本段/人员/选项/附件/日期等类型兼容）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        # 日期列为毫秒时间戳（量级 1e11+），金额等普通数字不做转换
        if value > 100_000_000_000:
            try:
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone(
                    timezone(timedelta(hours=8))
                ).strftime("%Y-%m-%d")
            except Exception:
                return str(value)
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_cell_to_str(v) for v in value]
        return "、".join(p for p in parts if p)
    if isinstance(value, dict):
        for k in ("text", "name", "en_name", "title"):
            if value.get(k):
                return str(value[k]).strip()
        return ""
    return str(value).strip()


async def _tenant_token(client: httpx.AsyncClient) -> str | None:
    if _token_cache["token"] and time.time() < _token_cache["expire_at"]:
        return _token_cache["token"]
    resp = await client.post(
        f"{OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    data = resp.json()
    if data.get("code") != 0:
        log.warning("飞书 tenant_access_token 获取失败: %s %s", data.get("code"), data.get("msg"))
        return None
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire_at"] = time.time() + int(data.get("expire", 7200)) - 300
    return _token_cache["token"]


async def _fetch_records(client: httpx.AsyncClient, token: str) -> list[dict] | None:
    records: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        resp = await client.get(
            f"{OPEN_API_BASE}/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{BITABLE_TABLE_ID}/records",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        data = resp.json()
        if data.get("code") != 0:
            log.warning("多维表格记录拉取失败: %s %s", data.get("code"), data.get("msg"))
            return None
        d = data.get("data") or {}
        records.extend(d.get("items") or [])
        if d.get("has_more"):
            page_token = d.get("page_token")
            continue
        break
    return records


async def run_sync_cycle() -> dict:
    """执行一轮同步：拉取全量记录 -> 按 PMP号 upsert projects。返回统计信息。"""
    global _warned_unconfigured
    if not _configured():
        if not _warned_unconfigured:
            log.warning("飞书多维表格同步未配置（缺少 FEISHU_APP_ID/FEISHU_APP_SECRET），同步器保持空闲")
            _warned_unconfigured = True
        return {"status": "disabled", "message": "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET"}

    async with httpx.AsyncClient(timeout=30) as client:
        token = await _tenant_token(client)
        if not token:
            return {"status": "error", "message": "tenant_access_token 获取失败"}
        records = await _fetch_records(client, token)
    if records is None:
        return {"status": "error", "message": "多维表格记录拉取失败"}

    stats: dict = {"total": len(records), "created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    unknown_fields: set[str] = set()

    async with async_session() as db:
        result = await db.execute(select(Project))
        by_pmp: dict[str, Project] = {p.pmp_no: p for p in result.scalars().all()}

        for rec in records:
            fields = rec.get("fields") or {}
            mapped: dict[str, str] = {}
            present: set[str] = set()   # 本记录中实际出现过的模型字段
            extra: dict[str, str] = {}
            for k, v in fields.items():
                s = _cell_to_str(v)
                key = k.replace(" ", "").replace("　", "").lower()
                attr = FIELD_MAP.get(key)
                if attr:
                    present.add(attr)
                    if s:
                        mapped[attr] = s
                elif s:
                    extra[k] = s
                    unknown_fields.add(k)

            pmp_no = (mapped.get("pmp_no") or "").strip()
            if not pmp_no:
                stats["skipped"] += 1
                continue

            project = by_pmp.get(pmp_no)
            if project is None:
                project = Project(pmp_no=pmp_no, tracked=False)
                for attr in SYNC_ATTRS:
                    if attr in present:
                        setattr(project, attr, mapped.get(attr) or None)
                if not project.name:
                    project.name = pmp_no
                project.extra_fields = extra or None
                db.add(project)
                by_pmp[pmp_no] = project
                stats["created"] += 1
                continue

            changed = False
            for attr in SYNC_ATTRS:
                if attr not in present:
                    continue   # 表格无此列：保留本地现有值，不覆盖
                new_val = mapped.get(attr) or None
                if getattr(project, attr) != new_val:
                    setattr(project, attr, new_val)
                    changed = True
            if (project.extra_fields or None) != (extra or None):
                project.extra_fields = extra or None
                changed = True
            stats["updated" if changed else "unchanged"] += 1

        await db.commit()

    if unknown_fields:
        log.info("未映射列（已原样存入 extra_fields）: %s", "、".join(sorted(unknown_fields)))
    return {"status": "ok", **stats}


async def _watch_loop() -> None:
    await asyncio.sleep(FIRST_DELAY_SECONDS)   # 避开应用启动窗口
    while True:
        _last["running"] = True
        try:
            result = await run_sync_cycle()
            _last.update(
                last_run_at=datetime.now(timezone.utc).isoformat(),
                last_result=result,
                last_error=None,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("多维表格同步周期失败")
            _last["last_error"] = str(e)
        finally:
            _last["running"] = False
        await asyncio.sleep(SYNC_SECONDS)


def start_watcher() -> None:
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        _watcher_task = asyncio.create_task(_watch_loop())


async def stop_watcher() -> None:
    global _watcher_task
    if _watcher_task is not None:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
        _watcher_task = None


def watcher_status() -> dict:
    return {
        "configured": _configured(),
        "app_token": BITABLE_APP_TOKEN,
        "table_id": BITABLE_TABLE_ID,
        "sync_seconds": SYNC_SECONDS,
        **_last,
    }

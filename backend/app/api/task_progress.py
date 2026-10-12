"""
任务进展模块 API。

- 项目管理：项目表格（种子数据）按行勾选追踪，勾选项目的关联邮箱邮件进入项目进展
- 项目进展：项目级 AI 聚合进展 + 单封邮件（原文 / AI 摘要）
- 待办事项：任务管理员发布给执行人/协助人的任务
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _is_admin, _is_project_manager
from app.core.security import get_current_user
from app.db.database import get_db
from app.db.models import MailProgress, Project, ProjectMailbox, ProjectProgress, TaskAuditLog, TodoTask, User
from app.models.schemas import (
    MailProgressListOut,
    MailProgressOut,
    MailSyncResult,
    ProjectMailboxCreate,
    ProjectMailboxOut,
    ProjectOut,
    ProjectProgressOut,
    ProjectTrackUpdate,
    TaskAuditLogOut,
    TodoTaskCreate,
    TodoTaskOut,
    TodoTaskUpdate,
    UserActionResponse,
)
from app.services import task_mail_service, feishu_bitable_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/task-progress", tags=["TaskProgress"])


async def get_current_project_manager(current_user: User = Depends(get_current_user)) -> User:
    if not _is_project_manager(current_user):
        raise HTTPException(status_code=403, detail="需要「任务管理员」权限")
    return current_user


# 任务字段中文名（操作日志展示用）
_TASK_FIELD_LABELS = {
    "title": "标题",
    "executor_id": "执行人",
    "assistant_id": "协助人",
    "priority": "优先级",
    "deadline": "截止时间",
    "remark": "备注",
    "delivery_result": "交付结果",
    "is_done": "完成状态",
    "completed_at": "完成时间",
}


def _task_snapshot(task: TodoTask) -> dict:
    return {k: getattr(task, k) for k in _TASK_FIELD_LABELS}


def _fmt_field_value(field: str, value) -> str:
    if value is None:
        return "（空）"
    if field in ("deadline", "completed_at"):
        return _fmt(value) or "（空）"
    if field == "is_done":
        return "已完成" if value else "进行中"
    if field == "priority":
        return {"high": "高", "medium": "中", "low": "低"}.get(str(value), str(value))
    return str(value)


def _diff_changes(before: dict, after: dict) -> list[dict]:
    """对比快照，生成字段级变更明细（仅记录有变化的字段）。"""
    changes = []
    for field, label in _TASK_FIELD_LABELS.items():
        old, new = before.get(field), after.get(field)
        if old != new:
            changes.append({
                "field": field,
                "label": label,
                "old": _fmt_field_value(field, old),
                "new": _fmt_field_value(field, new),
            })
    return changes


async def _log_task_action(
    db: AsyncSession,
    *,
    task_id: int,
    task_title: str | None,
    action: str,
    actor: User,
    changes: list[dict] | None,
) -> None:
    """写入任务操作日志（与业务写入同事务提交）。"""
    db.add(TaskAuditLog(
        task_id=task_id,
        task_title=task_title,
        action=action,
        changes=changes,
        actor_id=actor.id,
        actor_name=actor.username,
    ))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail=f"时间格式不正确: {value}")


def _fmt(dt) -> str | None:
    return dt.isoformat() if dt else None


# ---------------- 项目管理（项目表格勾选） ----------------

@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """项目表格全量（含追踪状态、邮件数、进展更新时间）。"""
    await task_mail_service.seed_projects(db)

    result = await db.execute(select(Project).order_by(Project.pmp_no))
    projects = result.scalars().all()

    mail_counts = dict(
        (row[0], row[1]) for row in
        (await db.execute(select(MailProgress.pmp_no, func.count(MailProgress.id)).group_by(MailProgress.pmp_no))).all()
    )
    progress_times = dict(
        (row[0], row[1]) for row in
        (await db.execute(select(ProjectProgress.pmp_no, ProjectProgress.updated_at))).all()
    )
    mailbox_counts = dict(
        (row[0], row[1]) for row in
        (await db.execute(select(ProjectMailbox.pmp_no, func.count(ProjectMailbox.id)).group_by(ProjectMailbox.pmp_no))).all()
    )

    return [
        ProjectOut(
            pmp_no=p.pmp_no,
            name=p.name,
            stage=p.stage,
            priority=p.priority,
            project_type=p.project_type,
            shipowner=p.shipowner,
            shipyard=p.shipyard,
            manager=p.manager,
            follow_up=p.follow_up,
            tracked=bool(p.tracked),
            mail_count=mail_counts.get(p.pmp_no, 0),
            progress_updated_at=_fmt(progress_times.get(p.pmp_no)),
        )
        for p in projects
    ]


@router.put("/projects/{pmp_no}/track", response_model=ProjectOut)
async def track_project(
    pmp_no: str,
    payload: ProjectTrackUpdate,
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_project_manager),
):
    """勾选/取消追踪项目：勾选后其关联邮箱的新邮件进入项目进展。"""
    result = await db.execute(select(Project).where(Project.pmp_no == pmp_no))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if payload.tracked and not project.tracked:
        # 新勾选追踪：归零水位线下轮全量重扫，补回该项目的历史邮件（幂等）
        task_mail_service.reset_scan_watermarks()
    project.tracked = payload.tracked
    await db.commit()
    return ProjectOut(
        pmp_no=project.pmp_no,
        name=project.name,
        stage=project.stage,
        priority=project.priority,
        project_type=project.project_type,
        shipowner=project.shipowner,
        shipyard=project.shipyard,
        manager=project.manager,
        follow_up=project.follow_up,
        tracked=bool(project.tracked),
        mail_count=0,
        progress_updated_at=None,
    )


@router.get("/projects/{pmp_no}/mailboxes", response_model=list[ProjectMailboxOut])
async def list_project_mailboxes(
    pmp_no: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """项目关联邮箱（由任务管理员手动维护，同时写回 项目属性.txt 对应字段）。"""
    result = await db.execute(select(ProjectMailbox).where(ProjectMailbox.pmp_no == pmp_no))
    return [ProjectMailboxOut(pmp_no=m.pmp_no, email_address=m.email_address) for m in result.scalars().all()]


@router.post("/projects/{pmp_no}/mailboxes", response_model=ProjectMailboxOut)
async def add_project_mailbox(
    pmp_no: str,
    payload: ProjectMailboxCreate,
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_project_manager),
):
    """手动添加项目关联邮箱（属性文件未覆盖的收件方可在此补充）。"""
    result = await db.execute(select(Project).where(Project.pmp_no == pmp_no))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if "@" not in payload.email_address:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    await task_mail_service.add_project_mailbox(db, pmp_no, payload.email_address)
    return ProjectMailboxOut(pmp_no=pmp_no, email_address=payload.email_address.strip().lower())


@router.delete("/projects/{pmp_no}/mailboxes", response_model=UserActionResponse)
async def remove_project_mailbox(
    pmp_no: str,
    email: str = Query(..., min_length=3),
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_project_manager),
):
    """手动移除项目关联邮箱（加入抑制列表，属性文件同步不会加回）。"""
    await task_mail_service.remove_project_mailbox(db, pmp_no, email)
    return UserActionResponse(status="deleted", message=f"已移除关联邮箱 {email}")


# ---------------- 项目进展 ----------------

@router.get("/progress", response_model=MailProgressListOut)
async def list_progress(
    pmp: str | None = None,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=100, le=300),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """单封邮件记录（原文 + AI 摘要），支持项目/关键词/时间段筛选，时间倒序。

    keyword 匹配主题/发件人/收件人/AI 摘要/原文；date_from/date_to 按自然日（含当天）。
    """
    conditions = []
    if pmp:
        conditions.append(MailProgress.pmp_no == pmp)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                MailProgress.subject.ilike(like),
                MailProgress.sender.ilike(like),
                MailProgress.recipients.ilike(like),
                MailProgress.ai_summary.ilike(like),
                MailProgress.original_text.ilike(like),
            )
        )
    if date_from:
        dt_from = _parse_dt(date_from)
        if dt_from:
            conditions.append(MailProgress.mail_date >= dt_from)
    if date_to:
        dt_to = _parse_dt(date_to)
        if dt_to:
            conditions.append(MailProgress.mail_date < dt_to + timedelta(days=1))

    total = await db.scalar(select(func.count()).select_from(MailProgress).where(*conditions)) or 0
    result = await db.execute(
        select(MailProgress)
        .where(*conditions)
        .order_by(MailProgress.mail_date.desc())
        .offset(offset)
        .limit(limit)
    )
    return MailProgressListOut(
        items=[
            MailProgressOut(
                id=r.id,
                pmp_no=r.pmp_no,
                direction=r.direction,
                subject=r.subject,
                sender=r.sender,
                recipients=r.recipients,
                mail_date=_fmt(r.mail_date),
                original_text=r.original_text,
                ai_summary=r.ai_summary,
                created_at=_fmt(r.created_at),
            )
            for r in result.scalars().all()
        ],
        total=total,
    )


@router.get("/projects/{pmp_no}/progress", response_model=ProjectProgressOut)
async def get_project_progress(
    pmp_no: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """项目级 AI 聚合进展（之前的进展 + 最新邮件迭代生成）。"""
    result = await db.execute(select(ProjectProgress).where(ProjectProgress.pmp_no == pmp_no))
    row = result.scalar_one_or_none()
    return ProjectProgressOut(
        pmp_no=pmp_no,
        content=row.content if row else None,
        source_msg_id=row.source_msg_id if row else None,
        updated_at=_fmt(row.updated_at) if row else None,
    )


# ---------------- 邮件同步 ----------------

@router.post("/mail-sync/run", response_model=MailSyncResult)
async def run_mail_sync(
    manager: User = Depends(get_current_project_manager),
):
    """立即执行一轮邮件同步（任务管理员可用，便于发布后立即查看最新进展）。"""
    try:
        new_records = await task_mail_service.run_sync_cycle()
        return MailSyncResult(status="ok", message=f"同步完成，新增 {new_records} 条项目进展", new_records=new_records)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("手动邮件同步失败")
        return MailSyncResult(status="error", message=f"同步失败：{e}", new_records=0)


@router.get("/mail-sync/status")
async def mail_sync_status(
    user: User = Depends(get_current_user),
):
    return task_mail_service.watcher_status()


@router.get("/bitable-sync/status")
async def bitable_sync_status(
    user: User = Depends(get_current_user),
):
    """飞书多维表格 -> 项目主数据 同步器状态（configured=false 表示凭据未配置）。"""
    return feishu_bitable_service.watcher_status()


# ---------------- 用户选择（发布任务用） ----------------

@router.get("/users")
async def list_assignable_users(
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_project_manager),
):
    result = await db.execute(
        select(User).where(User.is_active == True, User.is_approved == True).order_by(User.created_at)
    )
    return [
        {"id": u.id, "username": u.username}
        for u in result.scalars().all()
    ]


# ---------------- 待办事项 ----------------

async def _task_out(db: AsyncSession, task: TodoTask) -> TodoTaskOut:
    user_map: dict[int, str] = {}
    ids = {task.creator_id, task.executor_id}
    if task.assistant_id:
        ids.add(task.assistant_id)
    result = await db.execute(select(User).where(User.id.in_(ids)))
    for u in result.scalars().all():
        user_map[u.id] = u.username
    return TodoTaskOut(
        id=task.id,
        title=task.title,
        creator_id=task.creator_id,
        creator_name=user_map.get(task.creator_id),
        executor_id=task.executor_id,
        executor_name=user_map.get(task.executor_id),
        assistant_id=task.assistant_id,
        assistant_name=user_map.get(task.assistant_id) if task.assistant_id else None,
        priority=task.priority,
        remark=task.remark,
        deadline=_fmt(task.deadline),
        delivery_result=task.delivery_result,
        is_done=bool(task.is_done),
        completed_at=_fmt(task.completed_at),
        created_at=_fmt(task.created_at),
    )


@router.get("/tasks", response_model=list[TodoTaskOut])
async def list_tasks(
    status: str = Query(default="all", pattern="^(all|pending|done)$"),
    scope: str = Query(default="mine", pattern="^(mine|all)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """任务列表。

    scope=mine（默认）：我发布的 / 我执行的 / 我协助的；
    scope=all：全部任务，仅任务管理员（管理任务界面）可用。
    """
    if scope == "all":
        if not _is_project_manager(user):
            raise HTTPException(status_code=403, detail="仅任务管理员可查看全部任务")
        stmt = select(TodoTask)
    else:
        stmt = select(TodoTask).where(or_(
            TodoTask.creator_id == user.id,
            TodoTask.executor_id == user.id,
            TodoTask.assistant_id == user.id,
        ))
    if status == "pending":
        stmt = stmt.where(TodoTask.is_done == False)
    elif status == "done":
        stmt = stmt.where(TodoTask.is_done == True)
    stmt = stmt.order_by(TodoTask.is_done, TodoTask.deadline.is_(None), TodoTask.deadline, TodoTask.created_at.desc())
    result = await db.execute(stmt)
    return [await _task_out(db, t) for t in result.scalars().all()]


@router.post("/tasks", response_model=TodoTaskOut)
async def create_task(
    payload: TodoTaskCreate,
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_project_manager),
):
    executor = await db.get(User, payload.executor_id)
    if not executor or not executor.is_active or not executor.is_approved:
        raise HTTPException(status_code=400, detail="执行人不存在或未通过审核")
    assistant = None
    if payload.assistant_id:
        assistant = await db.get(User, payload.assistant_id)
        if not assistant or not assistant.is_active or not assistant.is_approved:
            raise HTTPException(status_code=400, detail="协助人不存在或未通过审核")
    task = TodoTask(
        title=payload.title.strip(),
        creator_id=manager.id,
        executor_id=payload.executor_id,
        assistant_id=payload.assistant_id,
        priority=payload.priority,
        remark=payload.remark,
        deadline=_parse_dt(payload.deadline),
        is_done=False,
    )
    db.add(task)
    await db.flush()
    executor_name = executor.username if executor else str(payload.executor_id)
    priority_label = {"high": "高", "medium": "中", "low": "低"}.get(payload.priority, payload.priority)
    await _log_task_action(
        db,
        task_id=task.id,
        task_title=task.title,
        action="create",
        actor=manager,
        changes=[{"field": "", "label": "发布任务", "old": "", "new": f"执行人 {executor_name} · 优先级 {priority_label}"}],
    )
    await db.commit()
    await db.refresh(task)
    return await _task_out(db, task)


@router.put("/tasks/{task_id}", response_model=TodoTaskOut)
async def update_task(
    task_id: int,
    payload: TodoTaskUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = await db.get(TodoTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    is_pm = _is_project_manager(user)
    is_involved = (
        task.creator_id == user.id
        or task.executor_id == user.id
        or (task.assistant_id is not None and task.assistant_id == user.id)
    )
    if not (is_pm or is_involved):
        raise HTTPException(status_code=403, detail="无权操作该任务")

    # 待办界面（非任务管理员）：只能填交付结果 / 备注 / 提交任务，
    # 其余安排字段（标题/执行人/协助人/优先级/截止/完成时间）仅任务管理员可在管理任务界面修改。
    if not is_pm:
        touched = [k for k, v in payload.model_dump().items() if v is not None]
        forbidden = [k for k in touched if k not in ("delivery_result", "remark", "is_done")]
        if forbidden:
            raise HTTPException(status_code=403, detail="仅任务管理员可在管理任务界面修改任务安排")

    before = _task_snapshot(task)

    if payload.title is not None:
        task.title = payload.title.strip()
    if payload.executor_id is not None:
        executor = await db.get(User, payload.executor_id)
        if not executor or not executor.is_active or not executor.is_approved:
            raise HTTPException(status_code=400, detail="执行人不存在或未通过审核")
        task.executor_id = payload.executor_id
    if payload.assistant_id is not None:
        if payload.assistant_id:
            assistant = await db.get(User, payload.assistant_id)
            if not assistant or not assistant.is_active or not assistant.is_approved:
                raise HTTPException(status_code=400, detail="协助人不存在或未通过审核")
        task.assistant_id = payload.assistant_id
    if payload.priority is not None:
        task.priority = payload.priority
    if payload.deadline is not None:
        task.deadline = _parse_dt(payload.deadline)
    if payload.remark is not None:
        task.remark = payload.remark
    if payload.delivery_result is not None:
        task.delivery_result = payload.delivery_result
    if payload.is_done is not None:
        task.is_done = payload.is_done
        if payload.is_done and payload.completed_at is None and task.completed_at is None:
            task.completed_at = datetime.now(task.deadline.tzinfo) if task.deadline else datetime.now()
        if not payload.is_done:
            task.completed_at = None
    if payload.completed_at is not None:
        task.completed_at = _parse_dt(payload.completed_at)

    changes = _diff_changes(before, _task_snapshot(task))
    if changes:
        # 执行人/协助人记录为用户名（快照里是 ID）
        async def _user_name(uid) -> str:
            if uid is None:
                return "（空）"
            u = await db.get(User, uid)
            return u.username if u else str(uid)

        for ch in changes:
            if ch["field"] == "executor_id":
                ch["old"] = await _user_name(before.get("executor_id"))
                ch["new"] = await _user_name(task.executor_id)
            elif ch["field"] == "assistant_id":
                ch["old"] = await _user_name(before.get("assistant_id"))
                ch["new"] = await _user_name(task.assistant_id)
        await _log_task_action(
            db,
            task_id=task.id,
            task_title=task.title,
            action="update",
            actor=user,
            changes=changes,
        )

    await db.commit()
    await db.refresh(task)
    return await _task_out(db, task)


@router.delete("/tasks/{task_id}", response_model=UserActionResponse)
async def delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = await db.get(TodoTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _is_project_manager(user):
        raise HTTPException(status_code=403, detail="仅任务管理员可在管理任务界面删除任务")
    await _log_task_action(
        db,
        task_id=task.id,
        task_title=task.title,
        action="delete",
        actor=user,
        changes=[{"field": "", "label": "删除任务", "old": "", "new": task.title}],
    )
    await db.delete(task)
    await db.commit()
    return UserActionResponse(status="deleted", message="任务已删除")


@router.get("/tasks/{task_id}/logs", response_model=list[TaskAuditLogOut])
async def list_task_logs(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_project_manager),
):
    """任务操作日志（管理任务界面），仅任务管理员可见。"""
    result = await db.execute(
        select(TaskAuditLog)
        .where(TaskAuditLog.task_id == task_id)
        .order_by(TaskAuditLog.created_at.desc(), TaskAuditLog.id.desc())
    )
    return [
        TaskAuditLogOut(
            id=log.id,
            task_id=log.task_id,
            task_title=log.task_title,
            action=log.action,
            changes=log.changes,
            actor_name=log.actor_name,
            created_at=_fmt(log.created_at),
        )
        for log in result.scalars().all()
    ]

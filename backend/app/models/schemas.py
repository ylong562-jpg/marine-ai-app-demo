"""
Pydantic models for request/response validation.
"""
from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)


class UserLogin(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class RegisterResponse(BaseModel):
    status: str
    message: str
    username: str


class UserInfo(BaseModel):
    id: int
    username: str
    is_active: bool
    is_approved: bool
    is_admin: bool
    is_core: bool
    is_project_manager: bool = False


class PendingUser(BaseModel):
    id: int
    username: str
    created_at: str


class AllUser(BaseModel):
    id: int
    username: str
    plain_password: str | None = None
    is_active: bool
    is_approved: bool
    is_admin: bool
    is_core: bool
    is_project_manager: bool = False
    created_at: str


class UserActionResponse(BaseModel):
    status: str
    message: str


# ---------- 任务进展模块 ----------

class ProjectOut(BaseModel):
    pmp_no: str
    name: str
    stage: str | None = None
    priority: str | None = None
    project_type: str | None = None
    shipowner: str | None = None
    shipyard: str | None = None
    manager: str | None = None
    follow_up: str | None = None
    tracked: bool = False
    mail_count: int = 0
    progress_updated_at: str | None = None


class ProjectMailboxOut(BaseModel):
    pmp_no: str
    email_address: str


class ProjectMailboxCreate(BaseModel):
    email_address: str = Field(..., min_length=3, max_length=200)


class MailProgressOut(BaseModel):
    id: int
    pmp_no: str
    direction: str
    subject: str | None = None
    sender: str | None = None
    recipients: str | None = None
    mail_date: str | None = None
    original_text: str | None = None
    ai_summary: str | None = None
    created_at: str | None = None


class MailProgressListOut(BaseModel):
    items: list[MailProgressOut]
    total: int


class ProjectProgressOut(BaseModel):
    pmp_no: str
    content: str | None = None
    source_msg_id: str | None = None
    updated_at: str | None = None


class ProjectTrackUpdate(BaseModel):
    tracked: bool


class TodoTaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    executor_id: int
    assistant_id: int | None = None
    priority: str = Field(default="medium", pattern="^(high|medium|low)$")
    deadline: str | None = None
    remark: str | None = None


class TodoTaskUpdate(BaseModel):
    title: str | None = None
    executor_id: int | None = None
    assistant_id: int | None = None
    priority: str | None = Field(default=None, pattern="^(high|medium|low)$")
    deadline: str | None = None
    remark: str | None = None
    delivery_result: str | None = None
    is_done: bool | None = None
    completed_at: str | None = None


class TodoTaskOut(BaseModel):
    id: int
    title: str
    creator_id: int
    creator_name: str | None = None
    executor_id: int
    executor_name: str | None = None
    assistant_id: int | None = None
    assistant_name: str | None = None
    priority: str
    remark: str | None = None
    deadline: str | None = None
    delivery_result: str | None = None
    is_done: bool
    completed_at: str | None = None
    created_at: str | None = None


class TaskAuditLogOut(BaseModel):
    id: int
    task_id: int | None = None
    task_title: str | None = None
    action: str
    changes: list[dict] | None = None
    actor_name: str | None = None
    created_at: str | None = None


class MailSyncResult(BaseModel):
    status: str
    message: str
    new_records: int = 0

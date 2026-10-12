"""
SQLAlchemy ORM models.
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    plain_password = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_approved = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    is_project_manager = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    """项目主数据（来自项目表格），勾选 tracked 后其关联邮箱的邮件进入项目进展。"""

    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    pmp_no = Column(String, unique=True, index=True, nullable=False)   # 如 PMP0000000101
    name = Column(String, nullable=False)                               # 项目名称
    stage = Column(String, nullable=True)                               # 阶段
    priority = Column(String, nullable=True)                            # 高/中/低/极高
    project_type = Column(String, nullable=True)                        # New Building / Finance / SnP / Agency / investment
    shipowner = Column(String, nullable=True)                           # 船东/买方
    shipyard = Column(String, nullable=True)                            # 船厂/卖方
    manager = Column(String, nullable=True)                             # 项目负责人
    follow_up = Column(String, nullable=True)                           # 跟进人
    tracked = Column(Boolean, default=False)                            # 是否在项目进展中追踪
    extra_fields = Column(JSON, nullable=True)                          # 多维表格中未映射的其余列（JSON，原样保留）
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ProjectMailbox(Base):
    """项目关联邮箱（解析自 feishu/email 项目 projects/*/项目属性.txt 的「项目关联邮箱」列）。"""

    __tablename__ = "project_mailboxes"

    id = Column(Integer, primary_key=True, index=True)
    pmp_no = Column(String, ForeignKey("projects.pmp_no"), nullable=False, index=True)
    email_address = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("pmp_no", "email_address", name="uq_project_mailbox"),)


class MailProgress(Base):
    """单封邮件的处理记录：原文 + AI 摘要（项目进展板块「相关邮件」数据来源）。"""

    __tablename__ = "mail_progress"

    id = Column(Integer, primary_key=True, index=True)
    msg_id = Column(String, nullable=False, index=True)                 # 邮件消息ID
    pmp_no = Column(String, ForeignKey("projects.pmp_no"), nullable=False, index=True)
    direction = Column(String, nullable=False, default="收件")            # 收件 / 发件
    subject = Column(String, nullable=True)
    sender = Column(String, nullable=True)
    recipients = Column(String, nullable=True)
    mail_date = Column(DateTime(timezone=True), nullable=True)
    original_text = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("msg_id", "pmp_no", name="uq_mail_progress"),)


class ProjectProgress(Base):
    """项目级进展：由 之前的进展 + 最新邮件 经 AI 迭代生成的完整项目总结（每个项目一行）。"""

    __tablename__ = "project_progress"

    id = Column(Integer, primary_key=True, index=True)
    pmp_no = Column(String, ForeignKey("projects.pmp_no"), nullable=False, unique=True, index=True)
    content = Column(Text, nullable=True)
    source_msg_id = Column(String, nullable=True)                       # 触发本次更新的邮件
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TodoTask(Base):
    """待办任务：任务管理员发布到执行人/协助人的待办事项。"""

    __tablename__ = "todo_tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    executor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    assistant_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    priority = Column(String, nullable=False, default="medium")  # high / medium / low
    remark = Column(Text, nullable=True)
    deadline = Column(DateTime(timezone=True), nullable=True)      # 截止时间
    delivery_result = Column(Text, nullable=True)                   # 交付结果
    is_done = Column(Boolean, default=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)   # 完成时间
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TaskAuditLog(Base):
    """任务操作日志：任务管理员在管理任务界面的每次新增/修改/删除均留痕（字段级 old -> new）。

    task_id 不带外键约束——任务删除后日志仍保留；task_title 为标题快照。
    """

    __tablename__ = "task_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, index=True)
    task_title = Column(String, nullable=True)
    action = Column(String, nullable=False)            # create / update / delete
    changes = Column(JSON, nullable=True)              # [{"field","label","old","new"}]
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    actor_name = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

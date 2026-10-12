"""
任务进展 - 邮件归档扫描与 AI 处理服务。

数据源：feishu/email 项目 archive_mail.py --daemon 持续写入的本地归档目录
（mail_archive/收件、mail_archive/发件），每封邮件一个 txt（头信息 + 原文）。
在其"转发到飞书表格"流程（未启动）之外，新增一套自动化处理：

每个查询周期：
1. 扫描归档目录中"新出现"的邮件文件（mtime 水位线 + 消息ID 去重）；
2. 解析头信息，用发/收件人地址匹配「项目关联邮箱」（唯一数据源为 project_mailboxes 表，
   由用户在项目管理界面手动维护，并写回 projects/<PMP>/项目属性.txt 的对应字段），
   且项目已被勾选追踪（projects.tracked）；
3. 对单封邮件生成 AI 摘要（原文 + 摘要 = 一封邮件的完整记录）；
4. 项目最新进展：取该项目「最近窗口」（默认最近 15 天，不足 10 封则向前顺延补足）内的
   全部邮件交给 AI 重新生成最新进展总结（project_progress 表，每个项目一行，整体替换旧进展）。

项目主数据来自项目表格（project_seed.py），在项目管理弹窗中按行勾选。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.db.database import data_dir
from app.db.models import MailProgress, Project, ProjectMailbox, ProjectProgress
from app.services.project_seed import PROJECT_SEED

log = logging.getLogger("task_progress")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

FEISHU_EMAIL_DIR = os.getenv("FEISHU_EMAIL_DIR", "/path/to/feishu/email")
MAIL_ARCHIVE_DIR = os.getenv("MAIL_ARCHIVE_DIR", os.path.join(FEISHU_EMAIL_DIR, "mail_archive"))
PROJECTS_DIR = os.getenv("FEISHU_PROJECTS_DIR", os.path.join(FEISHU_EMAIL_DIR, "projects"))
STATE_PATH = os.path.join(data_dir, "task_mail_state.json")
# 按项目落盘的输出根目录：每个项目一个文件夹，存放 项目进展.md 与单封邮件（摘要+原文）
OUTPUT_ROOT = os.getenv("TASK_PROGRESS_DIR", os.path.join(data_dir, "task_progress"))

POLL_SECONDS = int(os.getenv("TASK_MAIL_POLL_SECONDS", "300"))
FIRST_DELAY_SECONDS = int(os.getenv("TASK_MAIL_FIRST_DELAY_SECONDS", "5"))  # 启动后延迟首周期，避开启动窗口
SCAN_OVERLAP_SECONDS = 300            # mtime 水位线向前回退（防边界遗漏，消息ID去重保证幂等）
LOOKBACK_HOURS = int(os.getenv("TASK_MAIL_LOOKBACK_HOURS", "72"))   # 首次运行回溯时长
MAX_MAILS_PER_CYCLE = int(os.getenv("TASK_MAIL_MAX_PER_CYCLE", "100"))
MAX_BODY_CHARS = int(os.getenv("TASK_MAIL_MAX_BODY_CHARS", "6000"))

# 项目最新进展：取最近 N 天内的全部邮件；不足 M 封则向前顺延补足至 M 封；
# 超出软上限 K 封时只取最近 K 封（保护 AI 调用）；每封正文截断至 C 字符。
PROGRESS_WINDOW_DAYS = int(os.getenv("TASK_PROGRESS_WINDOW_DAYS", "15"))
PROGRESS_WINDOW_MIN_MAILS = int(os.getenv("TASK_PROGRESS_WINDOW_MIN_MAILS", "10"))
PROGRESS_WINDOW_MAX_MAILS = int(os.getenv("TASK_PROGRESS_WINDOW_MAX_MAILS", "30"))
PROGRESS_WINDOW_MAIL_CHARS = int(os.getenv("TASK_PROGRESS_WINDOW_MAIL_CHARS", "3000"))

FOLDER_DIRECTIONS = (("收件", "收件"), ("发件", "发件"))

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 后台轮询使用独立引擎（NullPool）：与 Web 请求的连接池完全隔离，
# 避免长周期任务的连接异常影响在线请求。
TASK_DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{data_dir}/app.db")
_task_engine = create_async_engine(TASK_DATABASE_URL, poolclass=NullPool)
task_session = async_sessionmaker(_task_engine, class_=AsyncSession, expire_on_commit=False)

# 最近一次运行状态（内存态，供状态接口展示）
_last_run: dict = {"running": False, "last_run_at": None, "last_new_records": 0, "last_error": None}
_cycle_lock = asyncio.Lock()


# ---------------- 状态文件 ----------------

def _read_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt_dt(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


# ---------------- 项目种子与邮箱映射 ----------------

async def seed_projects(db: AsyncSession) -> None:
    """首次启动时灌入项目主数据；已存在则跳过。"""
    result = await db.execute(select(func.count(Project.id)))
    if result.scalar_one() > 0:
        return
    for pmp_no, name, stage, priority, ptype, owner, yard, manager, follow_up in PROJECT_SEED:
        db.add(Project(
            pmp_no=pmp_no, name=name, stage=stage, priority=priority,
            project_type=ptype, shipowner=owner, shipyard=yard,
            manager=manager, follow_up=follow_up, tracked=False,
        ))
    await db.commit()
    log.info("已灌入 %d 个项目", len(PROJECT_SEED))


def _extract_emails(text: str) -> list[str]:
    return [e.lower() for e in EMAIL_RE.findall(text or "")]


def _sync_mailboxes_from_attr_files() -> dict[str, set[str]]:
    """【已弃用】项目属性.txt 中的「项目关联邮箱」值不可靠，不再作为数据源。"""
    return {}


# ---------------- 项目属性.txt 邮箱字段写回 ----------------

def _normalize_email(email: str) -> str:
    """提取并规范化邮箱：兼容直接输入或「Name <addr>」格式。"""
    email = email.strip()
    m = EMAIL_RE.search(email)
    return m.group(0).lower() if m else email.lower()


_ATTR_KEY_RE = re.compile(r"^[\w\u4e00-\u9fff]+[：:]")


def _rewrite_attr_mailbox_field_sync(pmp_no: str, emails: list[str]) -> None:
    """把手动维护的关联邮箱写回 projects/<PMP>/项目属性.txt 的「项目关联邮箱」字段。

    - 文件已存在：替换该字段所在行块（从「项目关联邮箱」行到下一个属性列之前）
    - 文件不存在：创建项目文件夹并写入最小属性文件
    """
    value = "、".join(emails)
    new_line = f"项目关联邮箱:{value}"
    os.makedirs(os.path.join(PROJECTS_DIR, pmp_no), exist_ok=True)
    path = os.path.join(PROJECTS_DIR, pmp_no, "项目属性.txt")

    lines: list[str] = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []

    out: list[str] = []
    replaced = False
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("项目关联邮箱"):
            if not replaced:
                out.append(new_line)
                replaced = True
            # 跳过旧值行块：直到下一个属性列或分隔线
            i += 1
            while i < len(lines) and not _ATTR_KEY_RE.match(lines[i]) and not lines[i].startswith("="):
                i += 1
            continue
        out.append(ln)
        i += 1
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(new_line)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.replace(tmp, path)


async def rewrite_attr_mailbox_field(pmp_no: str) -> None:
    """以 project_mailboxes 表当前值为准，重写属性文件中的邮箱字段。

    数据库是唯一数据源；属性文件写回为尽力而为，失败仅告警不影响业务。
    """
    try:
        async with task_session() as db:
            result = await db.execute(
                select(ProjectMailbox).where(ProjectMailbox.pmp_no == pmp_no).order_by(ProjectMailbox.email_address)
            )
            emails = [m.email_address for m in result.scalars().all()]
        await asyncio.to_thread(_rewrite_attr_mailbox_field_sync, pmp_no, emails)
    except Exception as e:
        log.warning("写回 项目属性.txt 失败（%s，数据库已更新）: %s", pmp_no, e)


def reset_scan_watermarks() -> None:
    """收件/发件扫描水位线归零，下一轮同步全量重扫归档目录。

    用于新增关联邮箱、新勾选追踪项目：让绑定之前的历史邮件也能被匹配入库。
    (msg_id, pmp_no) 唯一约束保证重扫幂等，已入库的邮件不会重复处理。
    """
    state = _read_state()
    last_scan = state.setdefault("last_scan", {})
    last_scan["收件"] = 0.0
    last_scan["发件"] = 0.0
    _write_state(state)


async def add_project_mailbox(db: AsyncSession, pmp_no: str, email: str) -> None:
    """手动添加项目关联邮箱（DB 为唯一数据源，并写回属性文件对应字段）。"""
    email = _normalize_email(email)
    result = await db.execute(
        select(ProjectMailbox).where(
            ProjectMailbox.pmp_no == pmp_no,
            ProjectMailbox.email_address == email,
        )
    )
    if result.scalar_one_or_none() is None:
        db.add(ProjectMailbox(pmp_no=pmp_no, email_address=email))
        await db.commit()
        # 新绑定邮箱后历史邮件也应可归集：归零水位线触发下轮全量重扫（幂等）
        reset_scan_watermarks()
    await rewrite_attr_mailbox_field(pmp_no)


async def remove_project_mailbox(db: AsyncSession, pmp_no: str, email: str) -> None:
    """手动移除项目关联邮箱（DB 删除，并写回属性文件对应字段）。"""
    email = _normalize_email(email)
    result = await db.execute(
        select(ProjectMailbox).where(
            ProjectMailbox.pmp_no == pmp_no,
            ProjectMailbox.email_address == email,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    await rewrite_attr_mailbox_field(pmp_no)


async def purge_auto_imported_mailboxes() -> int:
    """一次性清除早期从属性文件自动导入的关联邮箱（那些值不可靠），仅执行一次。"""
    state = _read_state()
    if state.get("mailboxes_manual_only_v3"):
        return 0
    async with task_session() as db:
        result = await db.execute(select(ProjectMailbox))
        rows = result.scalars().all()
        n = len(rows)
        for r in rows:
            await db.delete(r)
        await db.commit()
    state["mailboxes_manual_only_v3"] = True
    _write_state(state)
    if n:
        log.info("已清除 %d 条自动导入的关联邮箱（改为手动维护）", n)
    return n


# ---------------- 归档邮件解析 ----------------

HEADER_KEYS = ("收到时间", "对方发出", "类型", "发件人", "收件人", "主题", "邮件附件", "消息ID", "邮件ID", "会话ID", "Outlook")


def parse_archive_file(path: str) -> dict | None:
    """解析 mail_archive 中的单封邮件 txt，返回头字段 + 原文。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return None
    header: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for ln in text.splitlines():
        if in_body:
            body_lines.append(ln)
            continue
        if ln.strip() == "原文：":
            in_body = True
            continue
        m = re.match(r"^(" + "|".join(HEADER_KEYS) + r")：(.*)$", ln)
        if m:
            header[m.group(1)] = m.group(2).strip()
    return {
        "header": header,
        "body": "\n".join(body_lines).strip(),
        "raw": text,
    }


def _header_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone(timedelta(hours=8)))
        except Exception:
            continue
    return None


# ---------------- AI 处理 ----------------

def _get_ai_client():
    """OpenAI 兼容客户端：优先 DashScope，其次 LLM_* 环境变量。未配置时返回 None。"""
    api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        return None, None
    try:
        from openai import OpenAI  # noqa: PLC0415
    except Exception:
        return None, None
    base_url = (
        os.getenv("TASK_AI_BASE_URL")
        or os.getenv("LLM_BASE_URL")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = os.getenv("TASK_AI_MODEL") or os.getenv("LLM_MODEL") or "qwen-plus"
    try:
        return OpenAI(api_key=api_key, base_url=base_url, timeout=60), model
    except Exception as e:  # pragma: no cover
        log.warning("AI 客户端初始化失败: %s", e)
        return None, None


def _chat(client, model: str, system: str, user: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


SUMMARY_PROMPT = (
    "你是邮件摘要助手。请用中文以要点形式简明概括给定邮件，保留关键人物、时间、数字、事项与行动要求，"
    "总长度不超过150字。直接输出摘要内容，不要寒暄、不要复述本指令。"
)

LATEST_PROGRESS_PROMPT = (
    "你是项目管理助手。你会收到一个项目的基本信息，以及该项目最近一批往来邮件（按时间从早到晚排列）。"
    "请基于这些邮件总结该项目的最新进展，严格按以下三段，每段 3~5 行要点、总量不超过300字：\n"
    "【进展概述】项目当前所处阶段、各方核心诉求或结论的变化\n"
    "【关键事项】涉及的人物、时间节点、数字、待确认事项\n"
    "【待跟进】下一步需要谁、在什么时间前完成什么动作，或存在的风险\n"
    "只总结邮件中实际出现的信息，不要臆测；较早邮件中已被后续邮件解决或取代的事项请更新状态而非重复罗列。"
    "只输出上述内容，不要寒暄、不要复述本指令。"
)


def _ai_summary_sync(subject: str, body_text: str) -> str:
    client, model = _get_ai_client()
    if client is None:
        return "（未配置 AI API Key，已跳过智能处理）"
    try:
        return _chat(client, model, SUMMARY_PROMPT, f"主题：{subject}\n\n正文：\n{(body_text or '')[:MAX_BODY_CHARS]}")
    except Exception as e:
        log.warning("AI 摘要失败: %s", e)
        return f"（AI 摘要生成失败：{e}）"


def _mail_sort_key(m: MailProgress):
    """邮件排序键：mail_date 优先，缺失时用落库时间；naive 视为 UTC。"""
    d = m.mail_date or m.created_at
    if d is None:
        d = datetime.min.replace(tzinfo=timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


async def select_progress_window(db: AsyncSession, pmp_no: str) -> list[MailProgress]:
    """项目最新进展的邮件窗口：

    优先取最近 PROGRESS_WINDOW_DAYS 天内（按邮件真实日期）的全部邮件；
    若不足 PROGRESS_WINDOW_MIN_MAILS 封，则向前顺延补足至该封数
    （优先有真实日期的邮件，日期缺失的仅作补充）；仍不足则取项目全部邮件。
    超出 PROGRESS_WINDOW_MAX_MAILS 封软上限时，只取最近 K 封。
    """
    result = await db.execute(select(MailProgress).where(MailProgress.pmp_no == pmp_no))
    mails = sorted(result.scalars().all(), key=_mail_sort_key)
    if not mails:
        return []
    dated = [m for m in mails if m.mail_date is not None]
    cutoff = datetime.now(timezone.utc) - timedelta(days=PROGRESS_WINDOW_DAYS)
    window = [m for m in dated if _mail_sort_key(m) >= cutoff]
    if len(window) < PROGRESS_WINDOW_MIN_MAILS:
        # 向前顺延：优先有真实日期的邮件，日期缺失的（按入库时间排序）仅作补充
        if len(dated) >= PROGRESS_WINDOW_MIN_MAILS:
            pool = dated
        else:
            pool = dated + [m for m in mails if m.mail_date is None]
        window = sorted(pool, key=_mail_sort_key)[-PROGRESS_WINDOW_MIN_MAILS:]
    if len(window) > PROGRESS_WINDOW_MAX_MAILS:
        window = window[-PROGRESS_WINDOW_MAX_MAILS:]
    return window


def _ai_latest_progress_sync(project: Project, mails: list[MailProgress]) -> str:
    client, model = _get_ai_client()
    if client is None:
        return "（未配置 AI API Key，已跳过智能处理）"
    info = (
        f"项目编号：{project.pmp_no}\n项目名称：{project.name}\n阶段：{project.stage or '—'}\n"
        f"船东/买方：{project.shipowner or '—'}\n船厂/卖方：{project.shipyard or '—'}\n"
        f"项目负责人：{project.manager or '—'}　跟进人：{project.follow_up or '—'}"
    )
    blocks = []
    for m in mails:
        dt = _fmt_dt(m.mail_date) or "（时间未知）"
        blocks.append(
            f"--- 邮件（{dt} · {m.direction}）---\n主题：{m.subject or '（无主题）'}\n{(m.original_text or '')[:PROGRESS_WINDOW_MAIL_CHARS]}"
        )
    user = (
        f"{info}\n\n====== 最近往来邮件（按时间从早到晚，共 {len(mails)} 封）======\n"
        + "\n\n".join(blocks)
    )
    try:
        return _chat(client, model, LATEST_PROGRESS_PROMPT, user)
    except Exception as e:
        log.warning("AI 最新进展生成失败: %s", e)
        return ""  # 失败由调用方保留旧进展


async def ai_summary(subject: str, body_text: str) -> str:
    return await asyncio.to_thread(_ai_summary_sync, subject, body_text)


async def ai_latest_progress(project: Project, mails: list[MailProgress]) -> str:
    return await asyncio.to_thread(_ai_latest_progress_sync, project, mails)


# ---------------- 按项目落盘输出 ----------------

def _safe_filename(text: str, limit: int = 60) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n]+', "_", text or "").strip(" ._") or "无主题"
    return text[:limit]


def _project_dir(pmp_no: str) -> str:
    path = os.path.join(OUTPUT_ROOT, pmp_no)
    os.makedirs(path, exist_ok=True)
    return path


def _write_mail_file_sync(
    pmp_no: str,
    direction: str,
    mail_date: datetime | None,
    subject: str,
    sender: str | None,
    recipients: str | None,
    msg_id: str,
    summary: str,
    original: str,
) -> None:
    """单封邮件落盘：头信息 + AI 摘要 + 原文，命名与 feishu/email 项目文件夹惯例一致。"""
    dt = mail_date or datetime.now(timezone.utc)
    name = f"{dt.strftime('%Y-%m-%d %H.%M')}-{direction}-{_safe_filename(subject)}.txt"
    path = os.path.join(_project_dir(pmp_no), name)
    header = "\n".join([
        "=" * 30,
        f"项目编号：{pmp_no}",
        f"时间：{dt.strftime('%Y-%m-%d %H:%M:%S')}",
        f"类型：{direction}",
        f"发件人：{sender or '—'}",
        f"收件人：{recipients or '—'}",
        f"主题：{subject}",
        f"消息ID：{msg_id}",
        "=" * 30,
    ])
    content = f"{header}\n\n【AI 摘要】\n{summary or '—'}\n\n【原文】\n{original or '—'}\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _write_progress_file_sync(project: Project, content: str, source_subject: str | None, updated_at: datetime) -> None:
    """项目级进展落盘：每个项目一个 项目进展.md，随每次更新重写。"""
    path = os.path.join(_project_dir(project.pmp_no), "项目进展.md")
    header = "\n".join([
        f"# {project.name}（{project.pmp_no}）项目进展",
        "",
        f"- 阶段：{project.stage or '—'}",
        f"- 船东/买方：{project.shipowner or '—'}",
        f"- 船厂/卖方：{project.shipyard or '—'}",
        f"- 负责人：{project.manager or '—'}　跟进人：{project.follow_up or '—'}",
        f"- 更新时间：{updated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 触发邮件：{source_subject or '—'}",
        "",
        "---",
        "",
    ])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header + (content or "") + "\n")
    os.replace(tmp, path)


# ---------------- 归档扫描同步 ----------------

async def _run_sync_cycle_impl() -> int:
    """执行一轮同步：扫描归档新邮件 -> AI 摘要 -> 项目进展聚合。返回新增邮件记录数。"""
    async with task_session() as db:
        await seed_projects(db)

        result = await db.execute(select(Project).where(Project.tracked == True))
        tracked = {p.pmp_no: p for p in result.scalars().all()}
        if not tracked:
            log.info("未勾选追踪任何项目，跳过本轮同步")
            return 0

        result = await db.execute(select(ProjectMailbox))
        addr_map: dict[str, str] = {}  # email(lower) -> pmp_no
        for m in result.scalars().all():
            addr_map.setdefault(m.email_address.lower(), m.pmp_no)

        state = _read_state()
        last_scan: dict = state.setdefault("last_scan", {})
        now = datetime.now(timezone.utc).timestamp()

        new_records = 0
        affected_pmps: set[str] = set()   # 本轮有新邮件的项目，扫描结束后统一重算最新进展
        for folder, direction in FOLDER_DIRECTIONS:
            folder_path = os.path.join(MAIL_ARCHIVE_DIR, folder)
            if not os.path.isdir(folder_path):
                continue
            watermark = last_scan.get(folder)
            if watermark is None:
                since_ts = now - LOOKBACK_HOURS * 3600
            else:
                since_ts = float(watermark) - SCAN_OVERLAP_SECONDS

            try:
                entries = []
                for name in os.listdir(folder_path):
                    if not name.endswith(".txt"):
                        continue
                    full = os.path.join(folder_path, name)
                    try:
                        mtime = os.path.getmtime(full)
                    except OSError:
                        continue
                    if mtime >= since_ts:
                        entries.append((mtime, full))
                entries.sort()
            except Exception as e:
                log.warning("扫描目录 %s 失败: %s", folder_path, e)
                continue

            max_mtime = 0.0
            for mtime, full in entries[: MAX_MAILS_PER_CYCLE * 5]:
                max_mtime = max(max_mtime, mtime)
                parsed = parse_archive_file(full)
                if not parsed:
                    continue
                header = parsed["header"]
                msg_id = header.get("邮件ID") or header.get("消息ID")
                if not msg_id:
                    continue

                addrs = set()
                for key in ("发件人", "收件人"):
                    addrs.update(_extract_emails(header.get(key) or ""))
                matched_pmp = sorted({addr_map[a] for a in addrs if a in addr_map and addr_map[a] in tracked})
                if not matched_pmp:
                    continue

                # 已处理过的 (msg_id, pmp) 组合跳过
                result = await db.execute(
                    select(MailProgress.pmp_no).where(MailProgress.msg_id == msg_id)
                )
                done_pmps = set(result.scalars().all())

                subject = header.get("主题") or "（无主题）"
                mail_date = _header_dt(header.get("收到时间") or header.get("对方发出"))
                original_text = parsed["raw"]
                summary = await ai_summary(subject, parsed["body"])

                for pmp_no in matched_pmp:
                    if pmp_no in done_pmps:
                        continue
                    db.add(MailProgress(
                        msg_id=msg_id,
                        pmp_no=pmp_no,
                        direction=direction,
                        subject=subject,
                        sender=header.get("发件人"),
                        recipients=header.get("收件人"),
                        mail_date=mail_date,
                        original_text=original_text,
                        ai_summary=summary,
                    ))
                    new_records += 1

                    affected_pmps.add(pmp_no)

                    # 按项目落盘：单封邮件（摘要+原文）；项目进展.md 在扫描循环结束后统一重写
                    await asyncio.to_thread(
                        _write_mail_file_sync,
                        pmp_no, direction, mail_date, subject,
                        header.get("发件人"), header.get("收件人"),
                        msg_id, summary, original_text,
                    )

                done_pmps.update(matched_pmp)

            # 文件夹扫描成功才推进水位线
            if max_mtime > 0:
                last_scan[folder] = max(last_scan.get(folder, 0), max_mtime)

        # 项目最新进展：对本轮有新邮件的项目，取「最近窗口」邮件重新生成（整体替换旧进展）
        for pmp_no in sorted(affected_pmps):
            project = tracked[pmp_no]
            window_mails = await select_progress_window(db, pmp_no)
            if not window_mails:
                continue
            new_content = await ai_latest_progress(project, window_mails)
            if not new_content.strip():
                log.warning("项目 %s 最新进展生成失败，保留旧进展", pmp_no)
                continue
            result = await db.execute(select(ProjectProgress).where(ProjectProgress.pmp_no == pmp_no))
            progress = result.scalar_one_or_none()
            now_dt = datetime.now(timezone.utc)
            source = f"最近{PROGRESS_WINDOW_DAYS}天窗口 · {len(window_mails)} 封邮件"
            if progress is None:
                db.add(ProjectProgress(pmp_no=pmp_no, content=new_content, source_msg_id=source))
            else:
                progress.content = new_content
                progress.source_msg_id = source
            await asyncio.to_thread(_write_progress_file_sync, project, new_content, source, now_dt)
            log.info("项目 %s 最新进展已更新（%s）", pmp_no, source)

        await db.commit()
        _write_state(state)
        log.info("本轮同步完成，新增邮件记录 %d 条", new_records)
        return new_records


async def run_sync_cycle() -> int:
    """加锁执行一轮同步（守护循环与手动触发共用，防止并发重复处理）。"""
    async with _cycle_lock:
        return await _run_sync_cycle_impl()


# ---------------- 后台轮询 ----------------

_watcher_task: asyncio.Task | None = None


async def _watch_loop():
    await asyncio.sleep(FIRST_DELAY_SECONDS)   # 避开应用启动窗口
    while True:
        _last_run["running"] = True
        try:
            _last_run["last_new_records"] = await run_sync_cycle()
            _last_run["last_error"] = None
        except Exception as e:  # 单轮失败不终止守护
            log.exception("邮件同步周期失败: %s", e)
            _last_run["last_error"] = str(e)
        finally:
            _last_run["last_run_at"] = datetime.now(timezone.utc).isoformat()
            _last_run["running"] = False
        await asyncio.sleep(POLL_SECONDS)


def start_watcher():
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        _watcher_task = asyncio.create_task(_watch_loop())
        log.info("任务进展邮件归档扫描已启动，周期 %ds", POLL_SECONDS)


async def stop_watcher():
    global _watcher_task
    if _watcher_task is not None:
        _watcher_task.cancel()
        _watcher_task = None
    await _task_engine.dispose()


def watcher_status() -> dict:
    return dict(_last_run)

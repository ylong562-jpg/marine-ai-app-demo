# -*- coding: utf-8 -*-
"""
人工矫正 - 词表矫正 API

统一操作 entity_registry.db 中的全部实体记录（含 shipyard）。
支持加载、AI 分组、编辑和保存，并同步本地与 MySQL 两份数据。
"""
import asyncio
import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from datetime import date, datetime
from typing import Any, Optional

import mysql.connector
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.auth import _is_admin
from app.core.config import get_settings
from app.core.security import get_current_user
from app.db.models import User
from app.news_engine.entity_deduplication import classify_entity_name
from app.news_engine.json_validator import extract_json_from_text

settings = get_settings()
router = APIRouter(prefix="/vocabulary", tags=["Correction Vocabulary"])

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NEWS_ENGINE_ROOT = os.path.join(PROJECT_ROOT, "app", "news_engine")
DATA_DIR = os.path.join(NEWS_ENGINE_ROOT, "data")
ENTITY_REGISTRY_PATH = os.path.join(DATA_DIR, "entity_registry.db")

# 词表仅保留 5 大主标签对应的实体字段
ALLOWED_ENTITY_TYPES = {
    "shipowner",
    "shipyard",
    "buyer",
    "seller",
    "charterer",
    "owner",
    "company",
    "parties",
}
YARD_TYPES = {"shipyard"}
ENTITY_TYPES_WITH_CODE = {"company", "shipowner", "seller", "buyer", "parties"}

# 宽泛/非特定实体名称正则：保存词表时拒绝此类名称
_OVERLY_BROAD_NAME_PATTERNS = [
    r"旗下",
    r"有关",
    r"某",
    r"各",
    r"相关",
    r"等\s*$",
    r"^(中国|国内)\s*(船厂|公司|船东|港口)",
    r"^(船厂|公司|船东|港口)$",
    r"^(中国|国内)(船舶|造船)$",
    r"\b(a|the|some|certain|chinese|domestic)\s+(shipyard|company|shipowner|port)\b",
]


def _is_allowed_entity_type(entity_type: str) -> bool:
    return bool(entity_type and entity_type.strip() in ALLOWED_ENTITY_TYPES)


def _is_overly_broad_name(name: str) -> bool:
    if not name:
        return False
    text = name.strip().lower()
    if not text:
        return False
    for pat in _OVERLY_BROAD_NAME_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def _extract_row_names(row: dict) -> list[str]:
    names: list[str] = []
    for key in ("canonical_name", "english_name", "abbreviation"):
        value = str(row.get(key) or "").strip()
        if value:
            names.append(value)
    names.extend([str(a).strip() for a in (row.get("mapped_names") or []) if str(a).strip()])
    return names


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


def _get_mysql_connection():
    try:
        return mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"数据库连接失败: {e}",
        )


# ---------------------------------------------------------------------------
# Helper: normalization / derivation
# ---------------------------------------------------------------------------

def _parse_aliases(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(a).strip() for a in parsed if str(a).strip()]
    except Exception:
        pass
    return []


def _json_aliases(aliases: list[str]) -> str:
    return json.dumps(aliases, ensure_ascii=False)


def _is_latin(text: str) -> bool:
    """判断字符串是否主要由拉丁字符组成（允许空格、数字、标点）。"""
    if not text or not text.strip():
        return False
    return not bool(re.search(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", text))


def _derive_english_name(canonical: str, aliases: list[str]) -> str:
    """从规范名和别名中推断英文名。"""
    canonical = canonical.strip()
    if _is_latin(canonical) and len(canonical) > 1:
        return canonical
    best = ""
    for alias in aliases:
        alias = alias.strip()
        if not _is_latin(alias) or len(alias) <= 2:
            continue
        if len(alias) > len(best):
            best = alias
    return best


def _derive_abbreviation(canonical: str, english: str, aliases: list[str]) -> str:
    """从别名中推断缩写（全大写、较短、仅 ASCII 字母）。"""
    candidates = set()
    for alias in aliases:
        alias = alias.strip()
        if not alias or alias in (canonical, english):
            continue
        core = alias.replace(".", "").replace(" ", "")
        if core.isupper() and core.isalpha() and core.encode("ascii", "ignore").decode() == core and 1 < len(alias) <= 12:
            candidates.add(alias)
    if candidates:
        return sorted(candidates, key=len)[0]
    return ""


# ---------------------------------------------------------------------------
# Helper: entity registry
# ---------------------------------------------------------------------------

def _load_entity_rows(
    entity_type_filter: str = "all",
    specificity_filter: Optional[str] = None,
) -> list[dict]:
    if not os.path.exists(ENTITY_REGISTRY_PATH):
        return []
    conn = sqlite3.connect(ENTITY_REGISTRY_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        sql = (
            "SELECT id, entity_type, canonical_name, english_name, aliases, frequency, created_at, last_seen, code, specificity "
            "FROM entities"
        )
        params: list[Any] = []
        conditions: list[str] = []
        if entity_type_filter and entity_type_filter != "all":
            conditions.append("entity_type = ?")
            params.append(entity_type_filter)
        if specificity_filter and specificity_filter in ("specific", "broad"):
            conditions.append("specificity = ?")
            params.append(specificity_filter)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY entity_type, canonical_name"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    results = []
    for r in rows:
        aliases = _parse_aliases(r.get("aliases"))
        # 优先使用专用 english_name 字段；若为空再从 aliases 推导
        stored_english = str(r.get("english_name") or "").strip()
        english = stored_english if stored_english else _derive_english_name(r["canonical_name"], aliases)
        abbreviation = _derive_abbreviation(r["canonical_name"], english, aliases)
        mapped = [a for a in aliases if a not in (english, abbreviation)]
        stored_specificity = str(r.get("specificity") or "").strip()
        specificity = stored_specificity if stored_specificity else classify_entity_name(r["canonical_name"], r["entity_type"])
        results.append({
            "id": r["id"],
            "entity_type": r["entity_type"],
            "canonical_name": r["canonical_name"],
            "english_name": english,
            "abbreviation": abbreviation,
            "mapped_names": mapped,
            "frequency": r.get("frequency") or 1,
            "code": r.get("code") or "",
            "specificity": specificity,
            "created_at": r.get("created_at") or "",
            "last_seen": r.get("last_seen") or "",
        })
    return results


def _ensure_entity_table(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            english_name TEXT,
            aliases TEXT DEFAULT '[]',
            frequency INTEGER DEFAULT 1,
            created_at TEXT,
            last_seen TEXT,
            code TEXT,
            specificity TEXT,
            UNIQUE(entity_type, canonical_name)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(entity_type)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_code ON entities(code)")

    # 兼容旧表：若 specificity 列不存在则添加
    try:
        cur.execute("ALTER TABLE entities ADD COLUMN specificity TEXT")
    except sqlite3.OperationalError:
        pass


def _generate_entity_code(cur: sqlite3.Cursor, entity_type: str) -> Optional[str]:
    if entity_type in YARD_TYPES:
        prefix = "PMY"
    elif entity_type in ENTITY_TYPES_WITH_CODE:
        prefix = "PME"
    else:
        return None
    cur.execute(
        "SELECT MAX(CAST(SUBSTR(code, 4, 8) AS INTEGER)) FROM entities WHERE code LIKE ?",
        (prefix + "%",),
    )
    row = cur.fetchone()
    seq = (row[0] or 0) + 1
    return f"{prefix}{seq:08d}X"


def _save_entity_vocabulary(rows: list[dict], changed_rows: list[dict] | None = None) -> dict:
    """保存 entity 词表到 SQLite，并同步到 MySQL normalized_entity_vocabulary。"""
    if not rows:
        raise HTTPException(status_code=400, detail="数据不能为空")

    for idx, row in enumerate(rows):
        if not row.get("entity_type"):
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 行缺少 entity_type")
        et = str(row["entity_type"]).strip()
        if not _is_allowed_entity_type(et):
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx + 1} 行 entity_type '{et}' 不在允许范围内，当前仅支持: {', '.join(sorted(ALLOWED_ENTITY_TYPES))}",
            )
        if not row.get("canonical_name") or not str(row["canonical_name"]).strip():
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 行缺少规范名称")

    seen: set[tuple[str, str]] = set()
    duplicates = []
    for row in rows:
        key = (row["entity_type"], str(row["canonical_name"]).strip())
        if key in seen:
            duplicates.append(f"{key[0]}/{key[1]}")
        seen.add(key)
    if duplicates:
        raise HTTPException(status_code=400, detail=f"存在重复规范名: {', '.join(duplicates)}")

    # 拒绝宽泛/非特定实体名称
    # 如果调用方提供了 changed_rows，则只校验实际发生变更的行，避免未修改的历史数据阻塞保存
    rows_to_validate = changed_rows if changed_rows else rows
    invalid_rows = []
    for idx, row in enumerate(rows_to_validate):
        broad_names = [n for n in _extract_row_names(row) if _is_overly_broad_name(n)]
        if broad_names:
            canonical = str(row.get("canonical_name") or "").strip() or "未命名"
            invalid_rows.append(
                f"第 {idx + 1} 行（{row.get('entity_type')}: {canonical}）包含宽泛名称: {', '.join(broad_names)}"
            )
    if invalid_rows:
        raise HTTPException(status_code=400, detail="\n".join(invalid_rows))

    rows = sorted(rows, key=lambda r: 0 if str(r.get("code") or "").strip() else 1)

    affected_types = {r["entity_type"] for r in rows}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(ENTITY_REGISTRY_PATH)
    cur = conn.cursor()
    try:
        _ensure_entity_table(cur)

        for et in affected_types:
            cur.execute("DELETE FROM entities WHERE entity_type = ?", (et,))

        for row in rows:
            et = row["entity_type"]
            canonical = str(row["canonical_name"]).strip()
            english = str(row.get("english_name") or "").strip()
            abbreviation = str(row.get("abbreviation") or "").strip()
            mapped = [str(a).strip() for a in (row.get("mapped_names") or []) if str(a).strip()]

            aliases: list[str] = []
            if english and english != canonical and english not in aliases:
                aliases.append(english)
            if abbreviation and abbreviation != canonical and abbreviation not in aliases:
                aliases.append(abbreviation)
            for a in mapped:
                if a != canonical and a not in aliases:
                    aliases.append(a)

            freq = int(row.get("frequency") or 1)
            code = str(row.get("code") or "").strip() or None
            if code is None and (et in YARD_TYPES or et in ENTITY_TYPES_WITH_CODE):
                code = _generate_entity_code(cur, et)

            specificity = str(row.get("specificity") or "").strip()
            if specificity not in ("specific", "broad"):
                specificity = classify_entity_name(canonical, et)

            cur.execute(
                """
                INSERT INTO entities (entity_type, canonical_name, english_name, aliases, frequency, created_at, last_seen, code, specificity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (et, canonical, english or "", _json_aliases(aliases), freq, now, now, code, specificity),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"保存 SQLite 失败: {e}")
    finally:
        conn.close()

    _sync_entity_vocabulary_to_mysql()
    return {"success": True, "saved": len(rows)}


# ---------------------------------------------------------------------------
# MySQL sync
# ---------------------------------------------------------------------------

def _sync_entity_vocabulary_to_mysql() -> None:
    if not os.path.exists(ENTITY_REGISTRY_PATH):
        return
    conn_local = sqlite3.connect(ENTITY_REGISTRY_PATH)
    conn_local.row_factory = sqlite3.Row
    rows = conn_local.execute("SELECT * FROM entities").fetchall()
    conn_local.close()

    conn = _get_mysql_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS normalized_entity_vocabulary (
                id INT AUTO_INCREMENT PRIMARY KEY,
                snapshot_date DATE NOT NULL,
                entity_id INT,
                entity_type VARCHAR(100) NOT NULL,
                canonical_name VARCHAR(500) NOT NULL,
                english_name VARCHAR(500),
                aliases JSON,
                frequency INT DEFAULT 1,
                created_at DATETIME,
                last_seen DATETIME,
                code VARCHAR(100),
                specificity VARCHAR(20),
                synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_snapshot_entity (snapshot_date, entity_type, canonical_name),
                KEY idx_code (code),
                KEY idx_snapshot (snapshot_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        # 兼容旧表：若 english_name/specificity 列不存在则添加
        for col_def in [
            "english_name VARCHAR(500) AFTER canonical_name",
            "specificity VARCHAR(20) AFTER code",
        ]:
            try:
                cur.execute(f"ALTER TABLE normalized_entity_vocabulary ADD COLUMN {col_def}")
            except Exception:
                pass
        snapshot = date.today()
        cur.execute("DELETE FROM normalized_entity_vocabulary WHERE snapshot_date = %s", (snapshot,))

        insert_sql = """
            INSERT INTO normalized_entity_vocabulary
            (snapshot_date, entity_id, entity_type, canonical_name, english_name, aliases, frequency, created_at, last_seen, code, specificity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                english_name = VALUES(english_name),
                aliases = VALUES(aliases),
                frequency = VALUES(frequency),
                created_at = VALUES(created_at),
                last_seen = VALUES(last_seen),
                code = VALUES(code),
                specificity = VALUES(specificity),
                synced_at = VALUES(synced_at)
        """
        for row in rows:
            aliases = row["aliases"]
            try:
                aliases_json = json.dumps(json.loads(aliases), ensure_ascii=False) if aliases else None
            except Exception:
                aliases_json = json.dumps([aliases], ensure_ascii=False) if aliases else None
            cur.execute(
                insert_sql,
                (
                    snapshot,
                    row["id"],
                    row["entity_type"],
                    row["canonical_name"],
                    row["english_name"] or "",
                    aliases_json,
                    row["frequency"],
                    row["created_at"],
                    row["last_seen"],
                    row["code"],
                    row.get("specificity") or "specific",
                ),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"同步 entity 词表到 MySQL 失败: {e}")
    finally:
        cur.close()
        conn.close()



# ---------------------------------------------------------------------------
# AI grouping
# ---------------------------------------------------------------------------

def _normalize_name_for_grouping(name: str) -> str:
    """去掉通用后缀/前缀/标点，用于相似度预筛选。"""
    if not name:
        return ""
    n = name.lower().strip()
    # 移除括号及其中内容
    n = re.sub(r"[（(].*?[）)]", "", n)
    # 移除常见通用词（中英文）
    generic = [
        "shipyard", "shipbuilding", "marine", "group", "holding", "co.,", "co.", "co", "ltd", "limited",
        "inc", "corp", "corporation", "company", "s.a.", "sa", "bv", "nv", "plc", "gmbh", "ag",
        "公司", "集团", "船厂", "造船", "船舶", "重工", "船务", "海工", "海洋工程", "工程",
        "有限", "责任", "股份", "实业", "发展", "控股", "总公司", "有限公司", "有限责任公司",
    ]
    for g in generic:
        n = n.replace(g, "")
    # 移除常见无意义字符
    n = re.sub(r"[.,;:\\/&\-_'\"()（）【】\[\]]", "", n)
    n = re.sub(r"\s+", "", n)
    return n.strip()


class _UnionFind:
    def __init__(self, items: list[str]):
        self.parent = {item: item for item in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[ry] = rx


def _find_candidate_components(names: list[str]) -> list[list[str]]:
    """基于规范化后的子串包含关系，找出可能重复的候选连通分量。"""
    normalized = {name: _normalize_name_for_grouping(name) for name in names}
    # 去掉规范化后为空的名称
    valid_names = [n for n in names if normalized[n]]
    uf = _UnionFind(valid_names)

    # O(n^2) 两两比较；n 通常在千级以内，可接受
    for i, a in enumerate(valid_names):
        na = normalized[a]
        if not na:
            continue
        for b in valid_names[i + 1 :]:
            nb = normalized[b]
            if not nb:
                continue
            # 相互包含或共享较长子串则视为候选
            if na in nb or nb in na:
                uf.union(a, b)
                continue
            # 再试一下共享长度 >= 4 的公共子串（作为兜底，适合中文简称）
            shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
            if len(shorter) >= 4 and shorter in longer:
                uf.union(a, b)

    groups: dict[str, list[str]] = {}
    for name in valid_names:
        root = uf.find(name)
        groups.setdefault(root, []).append(name)
    return [g for g in groups.values() if len(g) >= 2]


def _extract_json_array(text: str) -> Optional[list]:
    """从文本中提取最外层 JSON 数组，兼容 markdown、前后自然语言、截断等情况。"""
    if not text or not isinstance(text, str):
        return None
    cleaned = text.strip()

    # 1. 直接解析
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. 提取 markdown 代码块
    json_block_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    for match in re.findall(json_block_pattern, cleaned, re.IGNORECASE):
        try:
            parsed = json.loads(match.strip())
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue

    # 3. 通过平衡方括号提取数组
    start = cleaned.find("[")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        break
    return None


def _call_llm_for_one_batch(names: list[str], entity_type: Optional[str] = None) -> list[dict]:
    """对一批规范名调用 LLM 分组。"""
    if len(names) < 2:
        return []
    try:
        from app.shipyard_ai.ai_extract_shipyard_info import call_qwen_api
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"无法加载 LLM 调用模块: {e}")

    is_shipyard_focus = entity_type == "shipyard"
    json_format_hint = (
        "请仅返回一个 JSON 数组，不要有任何解释文字、不要添加 markdown 代码块。\n"
        "格式如下（必须使用英文双引号）：\n"
        '[{"group_name": "组名", "members": ["名称1", "名称2"], "reason": "简短原因"}]\n\n'
    )
    if is_shipyard_focus:
        prompt = (
            "你是一位海事情报专家。下面是一份船厂规范名称列表。"
            "请将明显指向同一造船厂/船坞/厂区的名称归为一组。\n"
            "分组原则：\n"
            "1. 必须是同一物理船厂，才可合并；\n"
            "2. 同一集团在不同省、不同市的子公司应视为不同船厂，不得合并；\n"
            "3. 地区存在包含关系时视为同一地点（如省与省内的市/区，例如‘江苏’与‘海门’、‘山东’与‘烟台’），可合并；\n"
            "4. 缩写与全称、带前缀/后缀变体、中英文不同写法可合并；\n"
            "5. 明显不同的独立主体不要归为一组。\n"
            + json_format_hint
            + "名称列表（每行一个）：\n" + "\n".join(names)
        )
    else:
        prompt = (
            "你是一位海事情报专家。下面是一份实体规范名称列表。"
            "请将明显指向同一主体或同一企业集团的名称归为一组。\n"
            "分组原则：\n"
            "1. 对于船厂类名称，仅当指向同一造船厂/船坞/厂区时才合并；同一集团在不同省/市的子公司应视为不同船厂，地区存在包含关系（如省与省内的市）时除外；\n"
            "2. 对于非船厂实体，同一公司/集团的不同写法、不同语言、带前缀/后缀变体、缩写与全称应归为一组；\n"
            "3. 明显不同的独立主体不要归为一组。\n"
            + json_format_hint
            + "名称列表（每行一个）：\n" + "\n".join(names)
        )

    try:
        raw = call_qwen_api(prompt=prompt, text="", model="qwen-plus", timeout=120)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM 调用失败: {e}")

    if not raw:
        raise HTTPException(status_code=500, detail="LLM 返回为空")

    # 先尝试提取 JSON 数组（分组接口的核心返回类型）
    groups = _extract_json_array(raw)
    if groups is None:
        # 兜底：尝试通用 JSON 提取器（兼容对象包装等情况）
        extracted = extract_json_from_text(raw)
        if isinstance(extracted, list):
            groups = extracted
        elif isinstance(extracted, dict):
            groups = extracted.get("groups") or extracted.get("result") or []
            if not isinstance(groups, list):
                raise HTTPException(status_code=500, detail=f"LLM 返回格式无法解析：未找到有效的 groups 数组\n原始内容: {raw[:800]}")
        else:
            raise HTTPException(status_code=500, detail=f"LLM 返回格式无法解析\n原始内容: {raw[:800]}")
    if not isinstance(groups, list):
        raise HTTPException(status_code=500, detail=f"LLM 返回格式无法解析：不是数组\n原始内容: {raw[:800]}")

    name_set = set(names)
    valid_groups = []
    for g in groups:
        members = [m for m in g.get("members", []) if m in name_set]
        if len(members) >= 2:
            valid_groups.append({
                "group_name": g.get("group_name", "未命名组"),
                "members": members,
                "reason": g.get("reason", ""),
            })
    return valid_groups


def _call_llm_for_groups(canonical_names: list[str], entity_type: Optional[str] = None) -> list[dict]:
    """调用 LLM 对规范名进行相似分组。数据量大时先本地预筛选候选连通分量，再分批调用 LLM。"""
    if not canonical_names:
        return []

    # 去重但保留原始顺序
    seen = set()
    unique_names = [n for n in canonical_names if not (n in seen or seen.add(n))]

    # 少量数据直接调用
    if len(unique_names) <= 80:
        return _call_llm_for_one_batch(unique_names, entity_type=entity_type)

    # 大量数据：先本地生成候选连通分量，再把多个小分量合并成 <= 80 的批次
    components = _find_candidate_components(unique_names)
    if not components:
        return []

    # 按大小排序，便于合并批次
    components.sort(key=lambda c: len(c))
    batches: list[list[str]] = []
    current: list[str] = []
    for comp in components:
        if len(comp) > 80:
            # 过大的连通分量直接截断，避免单批超时
            comp = comp[:80]
        if len(current) + len(comp) > 80:
            if current:
                batches.append(current)
            current = comp[:]
        else:
            current.extend(comp)
    if current:
        batches.append(current)

    all_groups: list[dict] = []
    for batch in batches:
        groups = _call_llm_for_one_batch(batch, entity_type=entity_type)
        all_groups.extend(groups)
    return all_groups



# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class VocabRow(BaseModel):
    id: Optional[int] = None
    entity_type: str
    canonical_name: str
    english_name: str = ""
    abbreviation: str = ""
    mapped_names: list[str] = Field(default_factory=list)
    frequency: int = 1
    code: str = ""
    specificity: Optional[str] = Field("specific", description="specific 或 broad")
    created_at: Optional[str] = None
    last_seen: Optional[str] = None


class SaveVocabularyRequest(BaseModel):
    rows: list[VocabRow]
    changed_rows: list[VocabRow] = Field(
        default_factory=list,
        description="实际发生变更的行，用于针对性校验；为空时仍校验全部行",
    )


class AiGroupRequest(BaseModel):
    rows: list[VocabRow]
    entity_type: Optional[str] = Field("all", description="当前词表筛选的 entity_type，all 表示全部")


# ---------------------------------------------------------------------------
# Async AI grouping jobs
# ---------------------------------------------------------------------------

_AI_GROUP_JOBS: dict[str, dict] = {}
_AI_GROUP_JOB_TTL = 3600  # 作业结果保留 1 小时


def _prune_ai_group_jobs() -> None:
    now = time.time()
    expired = [jid for jid, job in _AI_GROUP_JOBS.items() if now - job.get("created_at", 0) > _AI_GROUP_JOB_TTL]
    for jid in expired:
        del _AI_GROUP_JOBS[jid]


def _start_ai_group_job(rows: list[VocabRow], entity_type: Optional[str] = None) -> str:
    job_id = str(uuid.uuid4())
    _AI_GROUP_JOBS[job_id] = {
        "status": "pending",
        "result": None,
        "error": None,
        "created_at": time.time(),
    }

    names = [r.canonical_name for r in rows if r.canonical_name]

    async def _run() -> None:
        try:
            groups = await asyncio.to_thread(_call_llm_for_groups, names, entity_type)
            _AI_GROUP_JOBS[job_id]["status"] = "done"
            _AI_GROUP_JOBS[job_id]["result"] = groups
        except HTTPException as exc:
            _AI_GROUP_JOBS[job_id]["status"] = "failed"
            _AI_GROUP_JOBS[job_id]["error"] = exc.detail
        except Exception as exc:
            _AI_GROUP_JOBS[job_id]["status"] = "failed"
            _AI_GROUP_JOBS[job_id]["error"] = str(exc)

    asyncio.create_task(_run())
    return job_id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/entity")
async def get_vocabulary(
    entity_type: Optional[str] = Query("all", description="筛选 entity_type，all 表示全部"),
    specificity: Optional[str] = Query(None, description="筛选具体/宽泛实体：specific、broad 或 all"),
    current_user: User = Depends(_require_admin),
):
    """加载实体词表（含 shipyard）。"""
    data = _load_entity_rows(
        entity_type_filter=entity_type or "all",
        specificity_filter=specificity,
    )
    return {"success": True, "count": len(data), "data": data}


@router.post("/entity")
async def save_vocabulary(
    req: SaveVocabularyRequest,
    current_user: User = Depends(_require_admin),
):
    """保存实体词表修改，同步本地 SQLite 与 MySQL。"""
    result = _save_entity_vocabulary(
        [r.model_dump() for r in req.rows],
        changed_rows=[r.model_dump() for r in req.changed_rows] if req.changed_rows else None,
    )
    return {"success": True, **result}


@router.post("/entity/ai-group")
async def ai_group_vocabulary(
    req: AiGroupRequest,
    current_user: User = Depends(_require_admin),
):
    """使用 AI 对当前词表进行相似分组（同步，数据量过大时容易超时）。"""
    names = [r.canonical_name for r in req.rows if r.canonical_name]
    groups = _call_llm_for_groups(names, entity_type=req.entity_type)
    return {"success": True, "count": len(groups), "groups": groups}


@router.post("/entity/ai-group/async")
async def ai_group_vocabulary_async(
    req: AiGroupRequest,
    current_user: User = Depends(_require_admin),
):
    """异步启动 AI 相似分组任务，立即返回 job_id 供轮询。"""
    _prune_ai_group_jobs()
    job_id = _start_ai_group_job(req.rows, entity_type=req.entity_type)
    return {"success": True, "job_id": job_id}


@router.get("/entity/ai-group/status/{job_id}")
async def ai_group_vocabulary_status(
    job_id: str,
    current_user: User = Depends(_require_admin),
):
    """查询异步 AI 分组任务状态。"""
    _prune_ai_group_jobs()
    job = _AI_GROUP_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {
        "success": True,
        "status": job["status"],
        "groups": job.get("result") if job["status"] == "done" else None,
        "count": len(job.get("result", [])) if job["status"] == "done" else 0,
        "error": job.get("error"),
    }

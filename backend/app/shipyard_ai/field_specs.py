"""
船厂字段规范中心。

所有与 `Time_Series.shipyard_data` 对应的字段格式、Prompt 说明、标准化/校验规则
在此集中定义，保证 Prompt、AI 输出、数据库导入三处一致。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


@dataclass
class FieldSpec:
    """单个数据库字段的完整规范。"""

    name: str
    db_type: str
    python_type: type
    stage: str
    description: str
    example: Any = None
    allowed_values: Optional[List[str]] = None
    allowed_synonyms: Optional[Dict[str, str]] = None
    format_type: str = "text"  # 见 FORMAT_REGISTRY
    is_json: bool = False
    include_in_prompt: bool = True
    required: bool = False
    default: Any = None
    extra_prompt: Optional[str] = None


# ---------------------------------------------------------------------------
# 标准化与校验辅助函数
# ---------------------------------------------------------------------------

def _extract_leading_number(value: Any) -> Optional[float]:
    """从字符串开头提取数字，去掉逗号和空格；返回 float 或 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    import re

    m = re.match(r"^([\d\s,\.]+)", s)
    if not m:
        return None
    num_str = m.group(1).replace(",", "").replace(" ", "")
    try:
        return float(num_str)
    except ValueError:
        return None


def _normalize_date(value: Any) -> Optional[str]:
    """把常见日期格式统一为 YYYY-MM-DD；仅年份则补 01-01。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")

    s = str(value).strip()
    if s.lower() in ("null", "none", "n/a", "-"):
        return None

    # 纯 4 位年份
    import re

    if re.match(r"^\d{4}$", s):
        return f"{s}-01-01"

    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _normalize_yn(value: Any) -> Optional[str]:
    """统一为 Y/N。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "Y" if value else "N"
    s = str(value).strip().upper()
    if s in ("Y", "YES", "TRUE", "1"):
        return "Y"
    if s in ("N", "NO", "FALSE", "0"):
        return "N"
    return None


def _normalize_enum(value: Any, allowed: List[str], synonyms: Optional[Dict[str, str]] = None) -> Optional[str]:
    """枚举值大小写不敏感匹配，支持同义词映射。"""
    if value is None or value == "":
        return None
    s = str(value).strip()
    lower_map = {a.lower(): a for a in allowed}
    canonical = lower_map.get(s.lower())
    if canonical is not None:
        return canonical
    if synonyms:
        syn = synonyms.get(s.lower())
        if syn is not None and syn.lower() in lower_map:
            return lower_map[syn.lower()]
    return None


# 集团字段规范值：COSCO（中远海运）、CSSC（中船集团）、CMG（招商船舶）、其它
_GROUP_OPTIONS = ["COSCO", "CSSC", "CMG", "其它"]
_GROUP_KEYWORDS: Dict[str, List[str]] = {
    "COSCO": ["中远", "cosco", "中远海运", "中国远洋", "远洋海运"],
    "CSSC": ["中船", "cssc", "中国船舶", "中船集团", "船舶集团", "中国船厂"],
    "CMG": ["招商", "cmg", "招商局", "招商船舶", "china merchants"],
}


def _classify_group_by_text(text: Optional[str]) -> Optional[str]:
    """根据文本关键词判断集团归属；无法判断时返回 None。"""
    if not text:
        return None
    lower = str(text).lower()
    for group, keywords in _GROUP_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in lower:
                return group
    return None


def _normalize_group(value: Any) -> Optional[str]:
    """
    集团字段标准化：仅允许 COSCO / CSSC / CMG / 其它。
    先尝试精确/同义词匹配，再扫描值文本关键词，最后兜底为 "其它"。
    """
    if value is None or value == "":
        return None
    s = str(value).strip()
    # 同义词映射（中文名 -> 代码）
    synonyms = {
        "中远海运": "COSCO",
        "中远": "COSCO",
        "cosco": "COSCO",
        "中船集团": "CSSC",
        "中国船舶": "CSSC",
        "中船": "CSSC",
        "cssc": "CSSC",
        "招商船舶": "CMG",
        "招商局": "CMG",
        "招商": "CMG",
        "cmg": "CMG",
    }
    canonical = synonyms.get(s.lower())
    if canonical:
        return canonical
    # 扫描文本关键词
    matched = _classify_group_by_text(s)
    if matched:
        return matched
    return "其它"


def _validate_group(value: Any) -> bool:
    return value is None or str(value) in _GROUP_OPTIONS


def _normalize_pure_number(value: Any) -> Optional[str]:
    """返回无单位、无逗号的数字字符串；整数不带小数点。"""
    num = _extract_leading_number(value)
    if num is None:
        return None
    if num == int(num):
        return str(int(num))
    return str(num)


def _normalize_semicolon_list(value: Any) -> Optional[str]:
    """用 ; 统一分隔列表，去掉空项。"""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        items = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return "; ".join(items) if items else None
    s = str(value).strip()
    if not s:
        return None
    import re

    items = [p.strip() for p in re.split(r"[;，,、]", s) if p.strip()]
    return "; ".join(items) if items else None


def _normalize_json_object(value: Any) -> Optional[dict]:
    import json

    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("null", "none"):
            return None
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _normalize_json_array(value: Any) -> Optional[list]:
    import json

    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("null", "none"):
            return None
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            return None
    return None


def _normalize_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _normalize_decimal(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _normalize_year(value: Any) -> Optional[int]:
    """从年份或日期提取 4 位年份。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        y = int(value)
        return y if 1000 <= y <= 9999 else None
    s = str(value).strip()
    import re

    m = re.match(r"^(\d{4})", s)
    if m:
        return int(m.group(1))
    return None


def _normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


# ---------------------------------------------------------------------------
# 校验函数
# ---------------------------------------------------------------------------

def _validate_date(value: Any) -> bool:
    import re

    if value is None:
        return True
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", str(value)))


def _validate_yn(value: Any) -> bool:
    return value in (None, "Y", "N")


def _validate_enum(value: Any, allowed: List[str]) -> bool:
    return value is None or str(value) in allowed


def _validate_pure_number(value: Any) -> bool:
    import re

    if value is None:
        return True
    return bool(re.match(r"^[\d]+(?:\.[\d]+)?$", str(value)))


def _validate_semicolon_list(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and bool(value.strip())


def _validate_json_object(value: Any) -> bool:
    return value is None or isinstance(value, dict)


def _validate_json_array(value: Any) -> bool:
    return value is None or isinstance(value, list)


def _validate_int(value: Any) -> bool:
    return value is None or isinstance(value, int)


def _validate_decimal(value: Any) -> bool:
    return value is None or isinstance(value, (int, float))


def _validate_year(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, int) and 1000 <= value <= 9999


def _validate_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


# ---------------------------------------------------------------------------
# 格式注册表：format_type -> (normalizer, validator)
# ---------------------------------------------------------------------------

FORMAT_REGISTRY: dict[str, tuple[Callable, Callable]] = {
    "text": (_normalize_text, _validate_text),
    "date": (_normalize_date, _validate_date),
    "year": (_normalize_year, _validate_year),
    "yn": (_normalize_yn, _validate_yn),
    "enum": (_normalize_enum, _validate_enum),
    "group": (_normalize_group, _validate_group),
    "pure_number": (_normalize_pure_number, _validate_pure_number),
    "semicolon_list": (_normalize_semicolon_list, _validate_semicolon_list),
    "json_object": (_normalize_json_object, _validate_json_object),
    "json_array": (_normalize_json_array, _validate_json_array),
    "int": (_normalize_int, _validate_int),
    "decimal": (_normalize_decimal, _validate_decimal),
    "custom": (lambda v: v, lambda v: True),  # 由调用方自行处理
}


# ---------------------------------------------------------------------------
# 字段定义
# ---------------------------------------------------------------------------

def _shipyard_fields() -> List[FieldSpec]:
    """返回 shipyard_data 所有业务字段的规范。"""
    return [
        # ===== basic =====
        FieldSpec(
            name="English_name",
            db_type="varchar(255)",
            python_type=str,
            stage="basic",
            description="船厂英文全称，使用专业海事英文术语",
            example="Yangzijiang Shipbuilding Group Ltd.",
        ),
        FieldSpec(
            name="Local_name",
            db_type="varchar(255)",
            python_type=str,
            stage="basic",
            description="船厂中文官方名称；若原文只有英文名，请根据知识给出准确中文",
            example="扬子江船业集团",
            extra_prompt="Local_name 必须是准确的中文官方名称，禁止直译。",
        ),
        FieldSpec(
            name="Abreviation",
            db_type="varchar(255)",
            python_type=str,
            stage="basic",
            description="船厂英文缩写，2-5 个大写字母，基于英文名称首字母",
            example="YZJ",
        ),
        FieldSpec(
            name="former_name",
            db_type="varchar(255)",
            python_type=str,
            stage="basic",
            description="曾用名/历史名称；中文船厂返回中文",
            example="扬子江船厂",
        ),
        FieldSpec(
            name="Shipyard_ID",
            db_type="varchar(100)",
            python_type=str,
            stage="basic",
            description="外部船厂编号，未提及则填 null",
            example=None,
            include_in_prompt=False,  # 不主动提取，仅保留字段
        ),
        FieldSpec(
            name="location",
            db_type="varchar(200)",
            python_type=str,
            stage="basic",
            description="地理位置，格式如 'City, Province/State'",
            example="Nantong, Jiangsu Province",
        ),
        FieldSpec(
            name="establishment_date",
            db_type="date",
            python_type=str,
            stage="basic",
            description="成立日期，格式 YYYY-MM-DD；若只有年份则使用 YYYY-01-01",
            example="2006-01-01",
            format_type="date",
        ),
        FieldSpec(
            name="country",
            db_type="varchar(100)",
            python_type=str,
            stage="basic",
            description="所在国家，英文",
            example="China",
        ),
        FieldSpec(
            name="state_owned",
            db_type="varchar(10)",
            python_type=str,
            stage="basic",
            description="是否有国有集团背景",
            example="N",
            format_type="yn",
        ),
        FieldSpec(
            name="is_active",
            db_type="varchar(10)",
            python_type=str,
            stage="basic",
            description="是否仍在运营；Y=运营，N=关闭/破产/合并",
            example="Y",
            format_type="yn",
            default="Y",
        ),
        FieldSpec(
            name="shipyard_type",
            db_type="varchar(50)",
            python_type=str,
            stage="basic",
            description="船厂类型",
            example="build/repair",
            format_type="enum",
            allowed_values=["build", "repair", "build/repair"],
            allowed_synonyms={
                "building": "build",
                "new build": "build",
                "newbuilding": "build",
                "new-building": "build",
                "construction": "build",
                "repairing": "repair",
                "repairs": "repair",
                "conversion": "repair",
                "conversions": "repair",
                "retrofit": "repair",
                "build & repair": "build/repair",
                "build and repair": "build/repair",
                "both": "build/repair",
                "building/repair": "build/repair",
                "newbuild/repair": "build/repair",
                "construction/repair": "build/repair",
                "build+repair": "build/repair",
            },
            default="build/repair",
        ),
        FieldSpec(
            name="group",
            db_type="varchar(200)",
            python_type=str,
            stage="basic",
            description="母公司/集团，仅允许：COSCO（中远海运）、CSSC（中船集团）、CMG（招商船舶）、其它",
            example="CSSC",
            format_type="group",
        ),
        FieldSpec(
            name="ubo",
            db_type="varchar(200)",
            python_type=str,
            stage="basic",
            description="最终实际控制人/母公司",
            example="CIMC Group",
        ),
        # ===== qualifications =====
        FieldSpec(
            name="qualification_certifications",
            db_type="text",
            python_type=str,
            stage="qualifications",
            description="资质认证列表（船级社、国际合规认证等），使用分号分隔",
            example="CCS certification; ABS certification; DNV certification",
            format_type="semicolon_list",
        ),
        FieldSpec(
            name="refund_guarantee_capacity",
            db_type="varchar(200)",
            python_type=str,
            stage="qualifications",
            description="是否具备退款担保能力",
            example="Y",
            format_type="yn",
        ),
        FieldSpec(
            name="annual_production_capacity",
            db_type="json",
            python_type=dict,
            stage="qualifications",
            description="年产能，JSON 对象包含 vessel（年造船数量/范围字符串）和 specs（年总 DWT 纯数字）",
            example={"vessel": "10-20", "specs": 3000000},
            is_json=True,
            format_type="custom",
        ),
        # ===== scale =====
        FieldSpec(
            name="shipyard_area",
            db_type="varchar(100)",
            python_type=str,
            stage="scale",
            description="船厂占地面积，单位平方米，仅返回纯数字",
            example="1360000",
            format_type="pure_number",
        ),
        FieldSpec(
            name="Quayside",
            db_type="varchar(100)",
            python_type=str,
            stage="scale",
            description="码头岸线长度，单位米，仅返回纯数字",
            example="2500",
            format_type="pure_number",
        ),
        # ===== facilities =====
        FieldSpec(
            name="production_lines",
            db_type="json",
            python_type=list,
            stage="facilities",
            description="生产线/船坞/船台列表，每个对象包含 no/type/capacity/dimensions/primary_function/is_active/cranes",
            example=[
                {
                    "no": 1,
                    "type": "dry dock",
                    "capacity": 30000,
                    "dimensions": "530 × 130 × 13.8",
                    "primary_function": "VLCC construction",
                    "is_active": "Y",
                    "cranes": [{"no": 1, "type": "gantry crane", "capacity": 900}],
                }
            ],
            is_json=True,
            format_type="custom",
        ),
        FieldSpec(
            name="crane_parameters",
            db_type="text",
            python_type=str,
            stage="facilities",
            description="吊机参数原始文本，例如 '2×900t gantry cranes'",
            example="2×900t gantry cranes",
        ),
        FieldSpec(
            name="Block_Assembly_Workshop",
            db_type="varchar(200)",
            python_type=str,
            stage="facilities",
            description="分段装配车间",
            example="2 workshops",
        ),
        FieldSpec(
            name="painting_workshop",
            db_type="varchar(200)",
            python_type=str,
            stage="facilities",
            description="涂装车间",
            example="1 workshop",
        ),
        FieldSpec(
            name="Outfitting_workshop",
            db_type="varchar(200)",
            python_type=str,
            stage="facilities",
            description="舾装车间",
            example="1 workshop",
        ),
        FieldSpec(
            name="other_facilities",
            db_type="text",
            python_type=str,
            stage="facilities",
            description="其他设施描述",
            example="Warehouses, offices",
        ),
        # ===== derived / not in prompt =====
        FieldSpec(
            name="founded_year",
            db_type="int",
            python_type=int,
            stage="derived",
            description="成立年份，可由 establishment_date 推导",
            example=2006,
            format_type="year",
            include_in_prompt=False,
        ),
        # ===== non-extracted DB fields =====
        FieldSpec(name="id", db_type="int", python_type=int, stage="meta", description="主键", include_in_prompt=False),
        FieldSpec(name="introduce", db_type="text", python_type=str, stage="meta", description="来源正文", include_in_prompt=False),
        FieldSpec(name="url", db_type="varchar(500)", python_type=str, stage="meta", description="来源链接", include_in_prompt=False),
        FieldSpec(name="crawled_at", db_type="timestamp", python_type=str, stage="meta", description="爬取时间", include_in_prompt=False),
        FieldSpec(name="ai_processed", db_type="tinyint(1)", python_type=int, stage="meta", description="AI 处理标记", include_in_prompt=False),
        FieldSpec(name="processed_at", db_type="timestamp", python_type=str, stage="meta", description="处理时间", include_in_prompt=False),
        FieldSpec(name="create_info", db_type="json", python_type=list, stage="meta", description="创建/更新历史", is_json=True, include_in_prompt=False),
        FieldSpec(name="update_time", db_type="varchar(20)", python_type=str, stage="meta", description="更新时间", include_in_prompt=False),
        FieldSpec(name="update_person", db_type="varchar(50)", python_type=str, stage="meta", description="更新人", include_in_prompt=False),
        FieldSpec(name="person_in_charge", db_type="varchar(50)", python_type=str, stage="meta", description="负责人", include_in_prompt=False),
        FieldSpec(name="is_verified", db_type="tinyint(1)", python_type=int, stage="meta", description="已核验标记", include_in_prompt=False),
        FieldSpec(name="lat", db_type="decimal(10,7)", python_type=float, stage="meta", description="纬度", include_in_prompt=False),
        FieldSpec(name="lng", db_type="decimal(11,7)", python_type=float, stage="meta", description="经度", include_in_prompt=False),
        FieldSpec(name="boundary_geo", db_type="json", python_type=dict, stage="meta", description="边界多边形", is_json=True, include_in_prompt=False),
        FieldSpec(name="shipbuilding_transactions", db_type="text", python_type=str, stage="meta", description="造船交易", include_in_prompt=False),
        FieldSpec(name="newbuilding_transactions", db_type="text", python_type=str, stage="meta", description="新船交易", include_in_prompt=False),
        FieldSpec(name="employees", db_type="int", python_type=int, stage="meta", description="员工数", include_in_prompt=False),
        FieldSpec(name="utilization_rate", db_type="decimal(5,4)", python_type=float, stage="meta", description="产能利用率", include_in_prompt=False),
        FieldSpec(name="orderbook_months", db_type="int", python_type=int, stage="meta", description="订单覆盖月数", include_in_prompt=False),
        FieldSpec(name="main_ship_types_now", db_type="text", python_type=str, stage="meta", description="当前主建船型", include_in_prompt=False),
        FieldSpec(name="ship_types_delivered", db_type="text", python_type=str, stage="meta", description="历史交付船型", include_in_prompt=False),
        FieldSpec(name="has_branch", db_type="varchar(1)", python_type=str, stage="meta", description="是否有分基地（Y/N）", example="N", format_type="yn", include_in_prompt=False),
        FieldSpec(name="branch_base", db_type="json", python_type=dict, stage="meta", description="分基地数据：坐标/范围/船坞几何", is_json=True, include_in_prompt=False),
    ]


# 单例缓存
_SHIPYARD_FIELD_SPECS: List[FieldSpec] = _shipyard_fields()
_SHIPYARD_FIELD_MAP: dict[str, FieldSpec] = {s.name: s for s in _SHIPYARD_FIELD_SPECS}


def get_field_specs() -> List[FieldSpec]:
    return list(_SHIPYARD_FIELD_SPECS)


def get_field_spec(name: str) -> Optional[FieldSpec]:
    return _SHIPYARD_FIELD_MAP.get(name)


def get_stage_fields(stage: str) -> List[FieldSpec]:
    return [s for s in _SHIPYARD_FIELD_SPECS if s.stage == stage and s.include_in_prompt]


def normalize_field(spec: FieldSpec, value: Any) -> Any:
    """按字段规范标准化单个值。"""
    if value is None:
        return None
    normalizer, _ = FORMAT_REGISTRY.get(spec.format_type, FORMAT_REGISTRY["text"])
    if spec.format_type == "enum":
        return normalizer(value, spec.allowed_values or [], spec.allowed_synonyms)
    return normalizer(value)


def validate_field(spec: FieldSpec, value: Any) -> tuple[bool, Optional[str]]:
    """校验单个值是否符合规范；返回 (是否通过, 错误信息)。"""
    _, validator = FORMAT_REGISTRY.get(spec.format_type, FORMAT_REGISTRY["text"])
    if spec.format_type == "enum":
        ok = validator(value, spec.allowed_values or [])
    else:
        ok = validator(value)
    if ok:
        return True, None
    return False, f"{spec.name} 格式无效: {value!r}（期望 {spec.format_type}）"

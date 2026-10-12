# -*- coding: utf-8 -*-
"""
Clarkson 订单数据 -> 维度目标分（1~100 百分位）。

重要约束：所有订单数字只来自 Clarkson 表（clarkson_orderbook，为空时回退
pmc_orderbook）+ pmc_builder_map；绝不使用 ship_news 中「新造船订单」的
交易数据（那些只作证据文本）。

目标分定义
----------
接单能力：订单量 / 金额综合百分位。
    - 金额按 currency 粗略归一到 USD（nb_price 单位为百万，JPYbn 为十亿）：
      $m×1, RMBm×0.14, EURm×1.08, GBPm×1.27, NOKm×0.095, AUDm×0.66, JPYbn×6.7
    - 金额覆盖率 >=30% 的船厂集合：y = 0.4×数量百分位 + 0.3×CGT百分位 + 0.3×金额百分位
      否则：y = 0.5×数量百分位 + 0.5×CGT百分位（避免价格缺失造成噪声）
交付履约：优先 clarkson_orderbook 已交付记录（status 非 On Order）数量百分位；
    当前库中若无已交付记录，则用 built（交船年份）分布的近期交付率：
    built 年份落在 [当前年-2, 当前年] 的订单占比；若全体船厂该比率全为 0，
    再回退到 contract_date 近 2 年内的接单占比（近期仍在运转的代理指标）。
技术实力：订单中 alternative_fuel_types 非空、或 eco_electronic_engine 非空、
    或 sox_scrubber_status='Fitted' 的订单占比百分位（绿色/环保技术船型占比）。
经营稳定性：contract_date 在最近 5 个自然年内的覆盖年数 / 5（接单时间分布连续性）。

fleet（clarkson_fleet）目标分（2026-10 加入）
------------------------------------------
clarkson_fleet 是船舶实际建造交付的地面真值（经 pmc_builder_map 对齐），
为回归目标引入四个指标，与 orderbook 目标按维度加权融合（fleet 缺失时
用可用部分归一化，全部缺失记 50 中性分）：

- 交付履约 = 0.7 × fleet + 0.3 × orderbook
    fleet: 近 5 个完整自然年（不含当年）年均建造艘数百分位
- 产能设施 = 1.0 × fleet（新 NNLS 维度；此前为先验维度）
    fleet: 近 5 个完整自然年建造 DWT 总量百分位（产能规模代理）
- 经营稳定性 = 0.5 × fleet + 0.5 × orderbook
    fleet: 近 8 个自然年（含当年）有建造活动的年数 / 8
- 技术实力 = 0.5 × fleet + 0.5 × orderbook
    fleet: 近 10 年建造船舶中绿色动力（alternative_fuel/eco engine/scrubber）占比百分位
- 接单能力：orderbook 口径不变（订单簿才是接单的恰当数据源）
"""
import re
from datetime import datetime
from typing import Optional

from . import db as dbmod

# 粗略汇率：把 nb_price 归一到百万美元
CURRENCY_TO_USD_M = {
    "$m": 1.0,
    "RMBm": 0.14,
    "EURm": 1.08,
    "GBPm": 1.27,
    "NOKm": 0.095,
    "AUDm": 0.66,
    "JPYbn": 6.7,
}


def _to_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _year(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.year
    m = re.search(r"(\d{4})", str(value))
    return int(m.group(1)) if m else None


def _percentiles(values: dict[int, float]) -> dict[int, float]:
    """把 {id: metric} 转成 1~100 的百分位分（rank pct）。"""
    if not values:
        return {}
    ranked = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ranked)
    if n == 1:
        return {ranked[0][0]: 50.0}
    result: dict[int, float] = {}
    for rank, (sid, _v) in enumerate(ranked):
        result[sid] = 1.0 + 99.0 * rank / (n - 1)
    return result


def load_orderbook(conn) -> list[dict]:
    """优先 clarkson_orderbook，为空/不存在则回退 pmc_orderbook。"""
    rows = dbmod.fetch_orderbook_rows(conn, "clarkson_orderbook")
    if not rows:
        rows = dbmod.fetch_orderbook_rows(conn, "pmc_orderbook")
    return rows


def load_fleet(conn) -> list[dict]:
    """clarkson_fleet 经 builder 映射对齐的建造记录。"""
    return dbmod.fetch_fleet_rows(conn)


def compute_fleet_targets(
    fleet_rows: list[dict], current_year: int
) -> dict[int, dict[str, float]]:
    """fleet 建造记录 -> 各 yard 的四个维度百分位目标分。

    返回 {shipyard_id: {"交付履约": p, "产能设施": p, "经营稳定性": p, "技术实力": p}}
    只含在 fleet 中有建造记录的船厂；指标定义见模块 docstring。
    """
    per_yard: dict[int, list[dict]] = {}
    for r in fleet_rows:
        sid = r.get("shipyard_id")
        if sid is None:
            continue
        per_yard.setdefault(int(sid), []).append(r)

    full_years = [current_year - 5, current_year - 1]  # 近5个完整自然年
    act_years = [current_year - 7, current_year]       # 近8个自然年（含当年）
    tech_years = [current_year - 9, current_year]      # 近10年

    delivery: dict[int, float] = {}     # 近5完整年年均建造艘数
    capacity: dict[int, float] = {}     # 近5完整年建造 DWT 总量
    activity: dict[int, float] = {}     # 近8年建造活动覆盖年数/8
    green_share: dict[int, float] = {}  # 近10年绿色动力占比

    for sid, rs in per_yard.items():
        full = [r for r in rs if (y := _year(r.get("built"))) and full_years[0] <= y <= full_years[1]]
        if full:
            delivery[sid] = len(full) / 5.0
            capacity[sid] = sum(_to_float(r.get("dwt")) or 0.0 for r in full)

        act = {
            y for r in rs
            if (y := _year(r.get("built"))) and act_years[0] <= y <= act_years[1]
        }
        activity[sid] = len(act) / 8.0

        recent = [r for r in rs if (y := _year(r.get("built"))) and tech_years[0] <= y <= tech_years[1]]
        if recent:
            tech = 0
            for r in recent:
                alt = (r.get("alternative_fuel_types") or "").strip()
                eco = (r.get("eco_electronic_engine") or "").strip()
                scrub = (r.get("sox_scrubber_status") or "").strip().lower()
                if alt or eco or scrub == "fitted":
                    tech += 1
            green_share[sid] = tech / len(recent)

    pct_delivery = _percentiles(delivery)
    pct_capacity = _percentiles(capacity)
    pct_activity = _percentiles(activity)
    pct_green = _percentiles(green_share)

    out: dict[int, dict[str, float]] = {}
    for sid in per_yard:
        out[sid] = {
            "交付履约": round(pct_delivery.get(sid, 50.0), 2),
            "产能设施": round(pct_capacity.get(sid, 50.0), 2),
            "经营稳定性": round(pct_activity.get(sid, 50.0), 2),
            "技术实力": round(pct_green.get(sid, 50.0), 2),
        }
    return out


# 维度融合权重：fleet 目标分占多少（其余归 orderbook；产能设施纯 fleet）
FLEET_BLEND = {
    "交付履约": 0.7,
    "产能设施": 1.0,
    "经营稳定性": 0.5,
    "技术实力": 0.5,
}


def compute_targets(
    rows: list[dict],
    current_year: int,
    fleet_rows: Optional[list[dict]] = None,
) -> dict[int, dict[str, float]]:
    """返回 {shipyard_id: {维度: 1~100 目标分}}，仅含 NNLS 维度（含产能设施）。

    orderbook 与 fleet 目标按 FLEET_BLEND 融合；任一部分缺失时按可用权重归一化。
    接单能力始终只用 orderbook 口径。
    """
    per_yard: dict[int, list[dict]] = {}
    for r in rows:
        sid = r.get("shipyard_id")
        if sid is None:
            continue
        per_yard.setdefault(int(sid), []).append(r)

    count = {sid: len(rs) for sid, rs in per_yard.items()}
    cgt = {sid: sum(_to_float(r.get("cgt")) or 0.0 for r in rs) for sid, rs in per_yard.items()}
    usd: dict[int, float] = {}
    priced = {sid: 0 for sid in per_yard}
    for sid, rs in per_yard.items():
        total = 0.0
        for r in rs:
            price = _to_float(r.get("nb_price"))
            rate = CURRENCY_TO_USD_M.get((r.get("currency") or "").strip())
            if price is not None and rate is not None:
                total += price * rate
                priced[sid] += 1
        usd[sid] = total

    # 交付履约
    delivered_count: dict[int, int] = {}
    recent_delivery_rate: dict[int, float] = {}
    contract_recency: dict[int, float] = {}
    for sid, rs in per_yard.items():
        delivered = 0
        recent_due = 0
        recent_contract = 0
        for r in rs:
            status = (r.get("status") or "").lower()
            if status and "on order" not in status:
                delivered += 1
            by = _year(r.get("built"))
            if by is not None and current_year - 2 <= by <= current_year:
                recent_due += 1
            cy = _year(r.get("contract_date"))
            if cy is not None and current_year - 2 <= cy <= current_year:
                recent_contract += 1
        delivered_count[sid] = delivered
        recent_delivery_rate[sid] = recent_due / len(rs)
        contract_recency[sid] = recent_contract / len(rs)

    use_delivered = sum(delivered_count.values()) > 0
    if use_delivered:
        delivery_metric = {sid: float(v) for sid, v in delivered_count.items()}
    elif any(v > 0 for v in recent_delivery_rate.values()):
        delivery_metric = recent_delivery_rate
    else:
        delivery_metric = contract_recency

    # 技术实力：绿色/环保技术订单占比
    tech_share: dict[int, float] = {}
    for sid, rs in per_yard.items():
        tech = 0
        for r in rs:
            alt = (r.get("alternative_fuel_types") or "").strip()
            eco = (r.get("eco_electronic_engine") or "").strip()
            scrub = (r.get("sox_scrubber_status") or "").strip().lower()
            if alt or eco or scrub == "fitted":
                tech += 1
        tech_share[sid] = tech / len(rs)

    # 经营稳定性：近 5 个自然年接单覆盖年数 / 5
    continuity: dict[int, float] = {}
    for sid, rs in per_yard.items():
        years = set()
        for r in rs:
            cy = _year(r.get("contract_date"))
            if cy is not None and current_year - 4 <= cy <= current_year:
                years.add(cy)
        continuity[sid] = len(years) / 5.0

    # 百分位
    pct_count = _percentiles({sid: float(v) for sid, v in count.items()})
    pct_cgt = _percentiles(cgt)
    pct_usd = _percentiles(usd)
    pct_delivery = _percentiles(delivery_metric)
    pct_tech = _percentiles(tech_share)
    pct_cont = _percentiles(continuity)

    # 金额覆盖率决定接单能力的合成方式
    priced_ratio = sum(1 for sid in per_yard if priced.get(sid, 0) >= max(1, 0.3 * count[sid]))
    use_usd = len(per_yard) > 0 and priced_ratio >= 0.5 * len(per_yard)

    targets: dict[int, dict[str, float]] = {}
    for sid in per_yard:
        if use_usd:
            order_score = 0.4 * pct_count[sid] + 0.3 * pct_cgt[sid] + 0.3 * pct_usd[sid]
        else:
            order_score = 0.5 * pct_count[sid] + 0.5 * pct_cgt[sid]
        targets[sid] = {
            "接单能力": round(order_score, 2),
            "交付履约": round(pct_delivery[sid], 2),
            "技术实力": round(pct_tech[sid], 2),
            "经营稳定性": round(pct_cont[sid], 2),
        }

    # ---- fleet 融合：NNLS 维度（含产能设施）与 fleet 目标分加权合并 ----
    fleet_targets = compute_fleet_targets(fleet_rows, current_year) if fleet_rows else {}
    all_sids = set(targets) | set(fleet_targets)
    merged: dict[int, dict[str, float]] = {}
    for sid in all_sids:
        ob = targets.get(sid) or {}
        fl = fleet_targets.get(sid) or {}
        row: dict[str, float] = {}
        if "接单能力" in ob:
            row["接单能力"] = ob["接单能力"]
        for dim, w_fleet in FLEET_BLEND.items():
            ob_part = ob.get(dim)
            fl_part = fl.get(dim)
            if ob_part is not None and fl_part is not None:
                row[dim] = round((1 - w_fleet) * ob_part + w_fleet * fl_part, 2)
            elif fl_part is not None:
                row[dim] = fl_part
            elif ob_part is not None:
                row[dim] = ob_part
            else:
                row[dim] = 50.0
        merged[sid] = row
    return merged

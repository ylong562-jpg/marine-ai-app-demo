# -*- coding: utf-8 -*-
"""
证据聚合与维度出分。

聚合方法（与用户方法论一致）：
- 时间衰减：evidence_weight = 0.5 ** (距今天数 / 365)（半衰期 365 天）
- 宣传打折：is_promo 证据的有效权重再乘 0.5
- 类别聚合分 = 加权平均分 × 数量饱和系数
      加权平均分 = Σ(score × weight) / Σ(weight)
      数量饱和   sat(n) = log1p(n) / log1p(20)，上限 1.0（20 条后不再加分）
- 维度得分 = Σ(类别聚合分 × 维度权重)，缺证据类别按 0 计入，最终 clip 到 1~100。
  （缺类别记 0 的定义与 NNLS 特征定义一致：证据少的船厂得分自然偏低，
    因为权重本身就是用同一特征定义对 Clarkson 目标拟合出来的。）
"""
import math
from datetime import date
from typing import Optional

from .dimensions import (
    CATEGORIES,
    HALF_LIFE_DAYS,
    PROMO_DISCOUNT,
    SATURATION_REF_COUNT,
)


def parse_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def time_decay(evidence_date, ref_date: date) -> float:
    d = parse_date(evidence_date)
    if d is None:
        d = ref_date
    age = max(0, (ref_date - d).days)
    return 0.5 ** (age / HALF_LIFE_DAYS)


def saturation(count: int, ref: int = SATURATION_REF_COUNT) -> float:
    if count <= 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(ref))


def evidence_weight(evidence: dict, ref_date: date) -> float:
    w = time_decay(evidence.get("date"), ref_date)
    if evidence.get("is_promo"):
        w *= PROMO_DISCOUNT
    return w


def aggregate_category(evidences: list[dict], ref_date: date) -> Optional[float]:
    """单个类别的聚合分（无证据返回 None）。evidence: {score, date, is_promo}"""
    if not evidences:
        return None
    num = 0.0
    den = 0.0
    for ev in evidences:
        w = evidence_weight(ev, ref_date)
        num += float(ev.get("score") or 0) * w
        den += w
    if den <= 0:
        return None
    weighted_mean = num / den
    return round(weighted_mean * saturation(len(evidences)), 2)


def aggregate_all_categories(
    evidences: list[dict], ref_date: date
) -> dict[str, Optional[float]]:
    """evidence 需含 category 字段；返回 {类别: 聚合分 or None}"""
    by_cat: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}
    for ev in evidences:
        cat = ev.get("category")
        if cat in by_cat:
            by_cat[cat].append(ev)
    return {cat: aggregate_category(evs, ref_date) for cat, evs in by_cat.items()}


def feature_vector(cat_aggs: dict[str, Optional[float]]) -> dict[str, float]:
    """NNLS 特征向量：缺证据类别记 0。"""
    return {cat: float(cat_aggs.get(cat) or 0.0) for cat in CATEGORIES}


def dimension_score(
    cat_aggs: dict[str, Optional[float]], dim_weights: dict[str, float]
) -> float:
    """维度得分 = Σ(类别聚合分 × 权重)，缺证据类别按 0，clip 1~100。"""
    total = 0.0
    for cat, w in (dim_weights or {}).items():
        agg = cat_aggs.get(cat)
        if agg is None:
            continue
        total += float(agg) * float(w)
    return round(min(100.0, max(1.0, total)), 1)


def effective_evidence_score(evidence: dict, ref_date: date) -> float:
    """证据对维度的有效贡献（用于挑选 top 证据）：score × 权重"""
    return float(evidence.get("score") or 0.0) * evidence_weight(evidence, ref_date)

# -*- coding: utf-8 -*-
"""
船厂六维评分系统。

六个互不交叉的维度（接口返回使用中文 key）：
    接单能力、交付履约、技术实力、经营稳定性、安全合规、产能设施

证据先由 LLM 按 7 个类别分类（一条新闻可属多个类别），逐条打分 1~100，
再按维度加权聚合（时间衰减 + 数量饱和 + 宣传打折），权重来自
Clarkson 订单数据推导目标的 NNLS 回归（无目标维度用专家先验）。
"""
from .dimensions import (
    MODEL_VERSION,
    DIMENSIONS,
    CATEGORIES,
    HALF_LIFE_DAYS,
    PROMO_DISCOUNT,
    SATURATION_REF_COUNT,
)

__all__ = [
    "MODEL_VERSION",
    "DIMENSIONS",
    "CATEGORIES",
    "HALF_LIFE_DAYS",
    "PROMO_DISCOUNT",
    "SATURATION_REF_COUNT",
]

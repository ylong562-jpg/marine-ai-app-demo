# -*- coding: utf-8 -*-
"""
维度权重的 NNLS 回归拟合与持久化。

对每个 NNLS 维度（接单能力/交付履约/技术实力/经营稳定性）：
    X = 各船厂「7 类别 × 聚合分」特征向量（无证据类别记 0）
    y = Clarkson 订单数据推导的目标分（1~100 百分位）
    scipy.optimize.nnls(X, y) -> w >= 0，归一化后作为维度权重。

无 Clarkson 目标的维度（安全合规/产能设施）使用 dimensions.PRIOR_WEIGHTS，
与拟合权重走同一条出分代码路径。

权重持久化在包内 data/weights.json，带 model_version 与拟合元信息。
"""
import json
import os
from typing import Optional

import numpy as np
from scipy.optimize import nnls

from .dimensions import (
    CATEGORIES,
    DIMENSIONS,
    MIN_FIT_SAMPLES,
    MODEL_VERSION,
    NNLS_DIMENSIONS,
    PRIOR_WEIGHTS,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WEIGHTS_PATH = os.path.join(DATA_DIR, "weights.json")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")


def render_matrix_markdown(weights: dict) -> str:
    """把权重文档渲染成人类可读的维度×类别矩阵表（留档用）。"""
    from datetime import datetime

    dims = (weights.get("dimensions") or {})
    header = "| 维度 | 来源 | 样本数 | " + " | ".join(CATEGORIES) + " |"
    sep = "|" + "---|" * (3 + len(CATEGORIES))
    lines = [
        f"# 船厂六维评分 NNLS 权重矩阵（{weights.get('model_version', '?')}）",
        f"",
        f"拟合时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"拟合源：clarkson_orderbook + clarkson_fleet 目标分（百分位）→ scipy NNLS，w≥0 归一化",
        f"",
        header,
        sep,
    ]
    for dim in (weights.get("dimension_order") or list(dims.keys())):
        entry = dims.get(dim) or {}
        w = entry.get("weights") or {}
        cells = " | ".join(f"{float(w.get(c) or 0.0):.3f}" for c in CATEGORIES)
        lines.append(
            f"| {dim} | {entry.get('source', '?')} | {entry.get('fitted_samples', 0)} | {cells} |"
        )
    lines.append("")
    lines.append("> 说明：每行权重和为 1；source=expert_prior* 为样本不足时的兜底先验。")
    return "\n".join(lines) + "\n"


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return {cat: (1.0 / len(CATEGORIES)) for cat in CATEGORIES}
    return {cat: w / total for cat, w in weights.items()}


def default_weights() -> dict:
    """未拟合时的兜底权重（全部维度用专家先验）。"""
    return {
        "model_version": MODEL_VERSION,
        "dimensions": {
            dim: {
                "weights": _normalize(PRIOR_WEIGHTS[dim]),
                "source": "expert_prior" if dim not in NNLS_DIMENSIONS else "expert_prior_fallback",
                "fitted_samples": 0,
            }
            for dim in DIMENSIONS
        },
    }


def load_weights(path: Optional[str] = None) -> dict:
    path = path or WEIGHTS_PATH
    if not os.path.exists(path):
        return default_weights()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default_weights()
    # 与当前维度/类别定义对齐：缺失维度用先验补齐
    base = default_weights()
    dims = data.get("dimensions") or {}
    for dim in DIMENSIONS:
        entry = dims.get(dim)
        if not entry or not isinstance(entry.get("weights"), dict):
            continue
        weights = {cat: float(entry["weights"].get(cat) or 0.0) for cat in CATEGORIES}
        base["dimensions"][dim] = {
            "weights": _normalize(weights),
            "source": entry.get("source", "expert_prior"),
            "fitted_samples": int(entry.get("fitted_samples") or 0),
        }
    base["model_version"] = data.get("model_version") or MODEL_VERSION
    return base


def save_weights(weights: dict, path: Optional[str] = None):
    """写入当前权重，并同步留档带时间戳快照（JSON + Markdown 矩阵表）。"""
    from datetime import datetime

    path = path or WEIGHTS_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(weights, f, ensure_ascii=False, indent=2)

    # 留档：archive/weights_<时间戳>.json + .md（历史版本可追溯、可回滚）
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(ARCHIVE_DIR, f"weights_{ts}.json"), "w", encoding="utf-8") as f:
            json.dump(weights, f, ensure_ascii=False, indent=2)
        with open(os.path.join(ARCHIVE_DIR, f"weights_{ts}.md"), "w", encoding="utf-8") as f:
            f.write(render_matrix_markdown(weights))
    except OSError:
        pass


def get_dimension_weights(weights: dict, dimension: str) -> dict[str, float]:
    entry = (weights.get("dimensions") or {}).get(dimension) or {}
    w = entry.get("weights") or {}
    result = {cat: float(w.get(cat) or 0.0) for cat in CATEGORIES}
    if sum(result.values()) <= 0:
        result = dict(PRIOR_WEIGHTS.get(dimension) or {})
    return _normalize(result)


def fit_weights(
    feature_rows: list[dict[str, float]],
    targets_by_dim: dict[str, list[float]],
) -> dict:
    """
    feature_rows: [{类别: 聚合分 or 0}, ...]（与 targets 对齐）
    targets_by_dim: {维度: [y, ...]}，仅 NNLS 维度会被拟合。
    返回新的权重文档（不直接落盘，由调用方 save_weights）。
    """
    weights = load_weights()
    X = np.array(
        [[float(row.get(cat) or 0.0) for cat in CATEGORIES] for row in feature_rows],
        dtype=float,
    )
    for dim in NNLS_DIMENSIONS:
        y_list = targets_by_dim.get(dim) or []
        n = min(len(y_list), X.shape[0])
        if n < MIN_FIT_SAMPLES:
            continue
        y = np.array(y_list[:n], dtype=float)
        try:
            w, _resid = nnls(X[:n], y)
        except Exception:
            continue
        w_dict = {cat: float(wi) for cat, wi in zip(CATEGORIES, w)}
        if sum(w_dict.values()) <= 0:
            continue
        weights["dimensions"][dim] = {
            "weights": _normalize(w_dict),
            "source": "nnls_clarkson_target",
            "fitted_samples": int(n),
        }
    weights["model_version"] = MODEL_VERSION
    return weights

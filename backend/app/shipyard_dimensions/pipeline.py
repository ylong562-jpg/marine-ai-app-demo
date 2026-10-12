# -*- coding: utf-8 -*-
"""
六维评分计算流水线：

    拉新闻 → LLM 分类(类别+is_promo) → LLM 逐类别打分(1~100)
    → 类别聚合(时间衰减+数量饱和+宣传打折)
    → 维度加权出分(NNLS 权重 / 专家先验，同一代码路径)
    → LLM 生成中文解读 → 写入 shipyard_dimension_scores 表。

全程只把 ship_news 当证据文本；所有订单数字来自 Clarkson 表。
"""
import asyncio
from datetime import date, datetime
from typing import Optional

from . import db as dbmod
from . import llm, scoring, targets, weights as weightsmod
from .dimensions import CATEGORIES, DIMENSIONS, MODEL_VERSION, NNLS_DIMENSIONS


def _shipyard_display_name(shipyard: dict) -> str:
    return (
        (shipyard.get("Local_name") or "").strip()
        or (shipyard.get("English_name") or "").strip()
        or str(shipyard.get("id"))
    )


async def collect_evidence(
    news_list: list[dict],
    name_variants: list[str],
    shipyard_name: str,
    progress_prefix: str = "",
) -> list[dict]:
    """新闻 → 证据列表 [{news_id, title, date, source_platform, category, score, is_promo}]"""
    evidence: list[dict] = []

    async def _handle(news: dict):
        label = f"{progress_prefix}[{news.get('id')}] {(news.get('title') or '')[:40]}"
        cls = await llm.classify_news(news, name_variants)
        if cls is None:
            print(f"  ⚠️ {label} 分类失败，跳过")
            return
        if not cls.get("related"):
            return
        cats = [c for c in (cls.get("categories") or []) if c in CATEGORIES]
        if not cats:
            return
        scores = await llm.score_evidence_batch(news, shipyard_name, cats)
        if not scores:
            # 批量打分失败：逐类别兜底一次
            for cat in cats:
                s = await llm.score_evidence(news, shipyard_name, cat)
                if s is not None:
                    scores[cat] = s
        for cat, score in scores.items():
            evidence.append(
                {
                    "news_id": news.get("id"),
                    "title": news.get("title") or "",
                    "date": news.get("date") or "",
                    "source_platform": news.get("source_platform") or "",
                    "category": cat,
                    "score": score,
                    "is_promo": bool(cls.get("is_promo")),
                }
            )
        print(f"  ✅ {label} 相关，类别 {list(scores.keys())}")

    sem = asyncio.Semaphore(4)

    async def _bounded(news: dict):
        async with sem:
            try:
                await _handle(news)
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ [{news.get('id')}] 处理异常: {e}")

    await asyncio.gather(*[_bounded(n) for n in news_list])
    return evidence


async def compute_shipyard_scores(
    conn,
    shipyard_id: int,
    weights_doc: dict,
    ref_date: Optional[date] = None,
    news_limit: int = 200,
    news_list: Optional[list[dict]] = None,
    name_variants: Optional[list[str]] = None,
    evidence: Optional[list[dict]] = None,
) -> dict:
    """
    对单家船厂完成 分类→打分→聚合→出分→解读。
    返回 {dimensions: {dim: {score, explanation, evidence_count, evidence_summary}},
          feature_vector, evidence_count, model_version}
    evidence 传入预计算结果时跳过新闻分类打分（fit_weights 阶段复用）。
    """
    ref_date = ref_date or date.today()
    shipyard = dbmod.get_shipyard(conn, shipyard_id)
    if shipyard is None:
        raise ValueError(f"船厂不存在: shipyard_id={shipyard_id}")
    shipyard_name = _shipyard_display_name(shipyard)
    if name_variants is None:
        name_variants = dbmod.get_name_variants(conn, shipyard)
    if news_list is None:
        news_list = dbmod.fetch_news_for_shipyard(conn, name_variants, limit=news_limit)

    print(f"🏭 {shipyard_name} (id={shipyard_id}): 召回新闻 {len(news_list)} 条")
    if evidence is None:
        evidence = await collect_evidence(news_list, name_variants, shipyard_name)

    cat_aggs = scoring.aggregate_all_categories(evidence, ref_date)
    feature = scoring.feature_vector(cat_aggs)

    dimensions_out: dict[str, dict] = {}
    for dim in DIMENSIONS:
        dim_weights = weightsmod.get_dimension_weights(weights_doc, dim)
        score = scoring.dimension_score(cat_aggs, dim_weights)
        # 该维度权重显著(>=0.1)的类别下的证据，按有效贡献取 top3
        relevant_cats = {c for c, w in dim_weights.items() if w >= 0.1}
        dim_evidence = [ev for ev in evidence if ev["category"] in relevant_cats]
        dim_evidence.sort(
            key=lambda ev: scoring.effective_evidence_score(ev, ref_date), reverse=True
        )
        top = dim_evidence[:3]
        if top:
            explanation = await llm.explain_dimension(dim, score, top)
        else:
            explanation = "暂无足够可验证的新闻证据支撑该维度评分。"
        if not explanation:
            explanation = "基于近期新闻证据聚合计算，证据详情见 evidence_summary。"
        dimensions_out[dim] = {
            "score": score,
            "explanation": explanation,
            "evidence_count": len(dim_evidence),
            "evidence_summary": {
                "category_scores": {c: a for c, a in cat_aggs.items() if a is not None},
                "top_evidence": [
                    {
                        "news_id": ev["news_id"],
                        "title": ev["title"],
                        "date": ev["date"],
                        "category": ev["category"],
                        "score": ev["score"],
                        "is_promo": ev["is_promo"],
                    }
                    for ev in top
                ],
            },
        }
        print(f"   📊 {dim}: {score} (证据 {len(dim_evidence)} 条)")

    return {
        "shipyard_id": shipyard_id,
        "shipyard_name": shipyard_name,
        "dimensions": dimensions_out,
        "feature_vector": feature,
        "evidence_count": len(evidence),
        "model_version": weights_doc.get("model_version") or MODEL_VERSION,
    }


def build_score_rows(result: dict, computed_at: Optional[datetime] = None) -> list[dict]:
    computed_at = computed_at or datetime.now()
    rows = []
    for dim, info in result["dimensions"].items():
        rows.append(
            {
                "dimension": dim,
                "score": info["score"],
                "explanation": info["explanation"],
                "evidence_count": info["evidence_count"],
                "evidence_summary": info["evidence_summary"],
                "model_version": result["model_version"],
                "computed_at": computed_at,
            }
        )
    return rows


async def run_pipeline(
    shipyard_ids: list[int],
    fit_weights: bool = False,
    news_limit: int = 200,
    write_db: bool = True,
    ref_date: Optional[date] = None,
) -> list[dict]:
    """
    小样本/全量共用入口。
    fit_weights=True 时：先对目标船厂集计算特征与 Clarkson 目标，NNLS 拟合
    权重并落盘 data/weights.json，再用新权重出分；样本不足时保留先验权重。
    每家船厂使用独立的数据库连接（读连接与写连接分离），任一家失败不影响其余，
    避免长时间 LLM 处理时连接超过服务端 wait_timeout 被断开导致整批丢失。
    """
    ref_date = ref_date or date.today()
    results: list[dict] = []
    # fit 阶段收集的证据按船厂缓存，主循环直接复用，避免重复 LLM 调用
    evidence_cache: dict[int, list[dict]] = {}

    weights_doc = weightsmod.load_weights()

    # 如需拟合权重：为每个有目标分的船厂收集特征（LLM 开销大，慎用）
    if fit_weights:
        print("🔧 fit_weights=True：开始计算 NNLS 特征与目标...")
        conn = dbmod.get_connection()
        try:
            order_rows = targets.load_orderbook(conn)
            fleet_rows = targets.load_fleet(conn)
        finally:
            conn.close()
        all_targets = targets.compute_targets(order_rows, ref_date.year, fleet_rows)
        # 只用五个 NNLS 维度全部有目标分的船厂：保证特征行与各维度 y 列表严格对齐
        fit_ids = [
            sid for sid in shipyard_ids
            if sid in all_targets
            and all(d in all_targets[sid] for d in NNLS_DIMENSIONS)
        ]
        feature_rows: list[dict[str, float]] = []
        target_rows: dict[str, list[float]] = {d: [] for d in all_targets.get(fit_ids[0], {})} if fit_ids else {}
        for sid in fit_ids:
            conn = dbmod.get_connection()
            try:
                shipyard = dbmod.get_shipyard(conn, sid)
                if not shipyard:
                    continue
                variants = dbmod.get_name_variants(conn, shipyard)
                news_list = dbmod.fetch_news_for_shipyard(conn, variants, limit=news_limit)
            finally:
                conn.close()
            if not news_list:
                continue
            evidence = await collect_evidence(
                news_list, variants, _shipyard_display_name(shipyard), progress_prefix=f"[fit {sid}] "
            )
            evidence_cache[sid] = evidence
            cat_aggs = scoring.aggregate_all_categories(evidence, ref_date)
            feature_rows.append(scoring.feature_vector(cat_aggs))
            for dim, y in all_targets[sid].items():
                target_rows.setdefault(dim, []).append(y)
        fitted = weightsmod.fit_weights(feature_rows, target_rows)
        weightsmod.save_weights(fitted)
        weights_doc = fitted
        print("🔧 权重已更新:", weightsmod.WEIGHTS_PATH)

    for sid in shipyard_ids:
        try:
            # 读取阶段：独立短连接（查询完成后即关闭）
            conn = dbmod.get_connection()
            try:
                cached = sid in evidence_cache
                result = await compute_shipyard_scores(
                    conn, sid, weights_doc, ref_date=ref_date, news_limit=news_limit,
                    evidence=evidence_cache.get(sid),
                )
            finally:
                conn.close()
            results.append(result)
            if write_db:
                # 写入阶段：全新连接，避免使用已闲置过久的读取连接
                wconn = dbmod.get_connection()
                try:
                    rows = build_score_rows(result)
                    dbmod.save_dimension_scores(wconn, sid, rows)
                    print(f"💾 shipyard_id={sid} 已写入 {len(rows)} 个维度得分")
                finally:
                    wconn.close()
        except Exception as e:  # noqa: BLE001
            print(f"❌ shipyard_id={sid} 处理失败: {e}")
    return results

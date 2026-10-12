# -*- coding: utf-8 -*-
"""
船厂六维评分全量/小样本计算入口。

用法（在 backend 目录下）：
    # 小范围试跑
    .venv/bin/python -m app.shipyard_dimensions --shipyard-id 423
    # 多家
    .venv/bin/python -m app.shipyard_dimensions --shipyard-id 423 --shipyard-id 106
    # 全部有订单映射的船厂（LLM 调用量大）
    .venv/bin/python -m app.shipyard_dimensions --all
    # 重新用 Clarkson 目标 NNLS 拟合权重（需先准备足够样本）
    .venv/bin/python -m app.shipyard_dimensions --all --fit-weights

选项：
    --shipyard-id ID   指定船厂（可多次传入，也支持逗号分隔）
    --all              所有有订单映射的船厂
    --fit-weights      计算 Clarkson 目标并 NNLS 拟合权重后出分
    --news-limit N     每家船厂最多处理的新闻条数（默认 200）
    --no-write         只计算不写库
    --dry-run          只拉新闻+分类统计，不打分
"""
import argparse
import asyncio
import sys

from . import db as dbmod
from .pipeline import run_pipeline


def _parse_ids(values: list[str]) -> list[int]:
    ids: list[int] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                ids.append(int(part))
    # 去重保序
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="船厂六维评分计算")
    parser.add_argument("--shipyard-id", action="append", default=[], help="船厂 ID（可多次或逗号分隔）")
    parser.add_argument("--all", action="store_true", help="全部有订单映射的船厂")
    parser.add_argument("--fit-weights", action="store_true", help="NNLS 重新拟合权重")
    parser.add_argument("--news-limit", type=int, default=200, help="每家船厂新闻条数上限")
    parser.add_argument("--no-write", action="store_true", help="不写数据库")
    parser.add_argument("--dry-run", action="store_true", help="只拉新闻并统计，不调用打分/解读")
    parser.add_argument("--skip-computed", action="store_true",
                        help="跳过当前 model_version 下六维已齐的船厂（中断后续跑）")
    args = parser.parse_args(argv)

    shipyard_ids = _parse_ids(args.shipyard_id)

    if args.dry_run:
        conn = dbmod.get_connection()
        try:
            if not shipyard_ids:
                rows = dbmod.list_shipyards_with_orders(conn)
                shipyard_ids = [r["id"] for r in rows]
            for sid in shipyard_ids:
                shipyard = dbmod.get_shipyard(conn, sid)
                if not shipyard:
                    print(f"⚠️ shipyard_id={sid} 不存在，跳过")
                    continue
                variants = dbmod.get_name_variants(conn, shipyard)
                news = dbmod.fetch_news_for_shipyard(conn, variants, limit=args.news_limit)
                print(f"[{sid}] {shipyard.get('Local_name') or shipyard.get('English_name')}: "
                      f"名称变体 {len(variants)} 个，召回新闻 {len(news)} 条")
        finally:
            conn.close()
        return

    if not shipyard_ids and not args.all:
        parser.error("请指定 --shipyard-id 或 --all（或 --dry-run）")

    if args.all:
        conn = dbmod.get_connection()
        try:
            rows = dbmod.list_shipyards_with_orders(conn)
            shipyard_ids = [r["id"] for r in rows]
            print(f"共 {len(shipyard_ids)} 家有订单映射的船厂")
            if args.skip_computed:
                from .dimensions import MODEL_VERSION
                done = dbmod.list_fully_computed_shipyards(conn, MODEL_VERSION)
                before = len(shipyard_ids)
                shipyard_ids = [sid for sid in shipyard_ids if sid not in done]
                print(f"断点续跑：跳过已完成 {before - len(shipyard_ids)} 家，剩余 {len(shipyard_ids)} 家")
        finally:
            conn.close()

    results = asyncio.run(
        run_pipeline(
            shipyard_ids,
            fit_weights=args.fit_weights,
            news_limit=args.news_limit,
            write_db=not args.no_write,
        )
    )
    print(f"\n完成，共计算 {len(results)} 家船厂")


if __name__ == "__main__":
    sys.exit(main())

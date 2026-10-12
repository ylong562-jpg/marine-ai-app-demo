# -*- coding: utf-8 -*-
"""
重新抓取 TradeWinds 低质量正文并重跑 AI 处理（一次性脏数据修复）。

背景：2026-09-17 之前入库的 TradeWinds 新闻正文包含大量页面噪音
（标题重复多份、作者/日期信息、话题标签、"Copy link/Be notified/Read more"
及相关推荐列表，或以 "html " 开头的 crawl4ai 抓取失败页）。
content_extractor._clean_tradewinds_text 已在提取链路中自动清洗新抓取的内容，
本脚本用于修复数据库中已存在的脏数据：
    重新抓取正文 -> 校验清洗结果 -> reprocess_single_news_by_id 重跑
    （重新分类 + 核心内容提取 + 报告生成，并更新 summary）。

用法：
    python refetch_reprocess_tradewinds.py                # 预览受影响的新闻
    python refetch_reprocess_tradewinds.py --execute      # 执行修复
    python refetch_reprocess_tradewinds.py --execute --ids 10218,10220
    python refetch_reprocess_tradewinds.py --days 30      # 扫描天数范围（默认 14）
"""
import argparse
import asyncio
import os
import re
import sys
from collections import Counter

_NE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_NE_ROOT, os.path.join(_NE_ROOT, "utils")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db_config import DB_CONFIG
from complete_news_pipeline import connect_to_database
from content_extractor import extract_article
from reprocess_database_news import reprocess_single_news_by_id

FOOTER_MARKERS = ("Copy link", "Be notified", "Read more")
DATE_PREFIX_RE = re.compile(r"^【该新闻的(?:发布)?日期是[^】]*】\s*")
MIN_CONTENT_LENGTH = 200


def strip_wrapper(text: str) -> str:
    return DATE_PREFIX_RE.sub("", (text or "").strip())


def junk_reason(text: str):
    """返回脏数据原因字符串，正常正文返回 None。"""
    t = strip_wrapper(text)
    if not t:
        return "正文为空"
    if t.lower().startswith("html "):
        return '抓取失败页（"html " 前缀）'
    if any(m in t for m in FOOTER_MARKERS):
        return "混入页脚/相关推荐区"
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    if lines:
        top, top_n = Counter(lines).most_common(1)[0]
        if top_n >= 3:
            return f"同一行重复 {top_n} 次（标题/署名重复）"
    return None


def validate_refetched(content: str) -> str | None:
    """校验重新抓取的正文，返回错误原因；通过返回 None。"""
    t = (content or "").strip()
    if len(t) < MIN_CONTENT_LENGTH:
        return f"正文过短（{len(t)} 字符）"
    if t.lower().startswith("html "):
        return '仍为抓取失败页（"html " 前缀）'
    if any(m in t for m in FOOTER_MARKERS):
        return "仍含页脚/相关推荐区"
    return None


def fetch_tradewinds_candidates(days: int, limit: int = 200):
    conn = connect_to_database(**DB_CONFIG)
    if not conn:
        raise RuntimeError("数据库连接失败")
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, title, link, date, summary
            FROM ship_news
            WHERE link LIKE '%%tradewindsnews%%'
              AND date >= DATE_SUB(CURDATE(), INTERVAL %d DAY)
            ORDER BY id DESC
            LIMIT %d
            """
            % (int(days), int(limit))
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    return [
        {**r, "reason": junk_reason(r.get("summary") or "")}
        for r in rows
        if junk_reason(r.get("summary") or "")
    ]


async def refetch_content(row: dict) -> str:
    result = await extract_article(row["link"], title=row.get("title") or "")
    if not result.get("success"):
        raise RuntimeError(f"抓取失败: {result.get('error') or '未知错误'}")
    err = validate_refetched(result.get("content") or "")
    if err:
        raise RuntimeError(f"正文校验未通过: {err}")
    return result["content"]


async def run(rows: list, fetch_concurrency: int = 2) -> dict:
    sem = asyncio.Semaphore(fetch_concurrency)
    contents: dict[int, str] = {}
    fetch_failed: dict[int, str] = {}

    async def fetch_guarded(row: dict):
        async with sem:
            try:
                contents[row["id"]] = await refetch_content(row)
                print(
                    f"  [抓取] id={row['id']} 成功，正文 {len(contents[row['id']])} 字符",
                    flush=True,
                )
            except Exception as e:
                fetch_failed[row["id"]] = str(e)
                print(f"  [抓取] id={row['id']} 失败: {e}", flush=True)

    print(f"第一步：并发 {fetch_concurrency} 重新抓取 {len(rows)} 篇文章...", flush=True)
    await asyncio.gather(*(fetch_guarded(r) for r in rows))

    print(f"\n第二步：重跑 AI 处理（抓取成功 {len(contents)} 篇，失败 {len(fetch_failed)} 篇）...", flush=True)
    reprocess_failed: dict[int, str] = {}
    done = 0
    for row in rows:
        nid = row["id"]
        if nid not in contents:
            continue
        try:
            res = await reprocess_single_news_by_id(nid, manual_summary=contents[nid])
            done += 1
            print(
                f"  [处理] id={nid} 成功 ({done}/{len(contents)}): {(res.get('title') or '')[:40]}",
                flush=True,
            )
        except Exception as e:
            reprocess_failed[nid] = str(e)
            print(f"  [处理] id={nid} 失败: {e}", flush=True)

    return {
        "total": len(rows),
        "fetched": len(contents),
        "reprocessed": done,
        "fetch_failed": fetch_failed,
        "reprocess_failed": reprocess_failed,
    }


def main():
    parser = argparse.ArgumentParser(description="TradeWinds 脏数据重新抓取与重处理")
    parser.add_argument("--execute", action="store_true", help="实际执行（默认仅预览）")
    parser.add_argument("--days", type=int, default=14, help="扫描最近 N 天（默认 14）")
    parser.add_argument("--limit", type=int, default=200, help="最多扫描条数（默认 200）")
    parser.add_argument("--ids", type=str, default="", help="只处理指定 id，逗号分隔")
    cli = parser.parse_args()

    print(f"扫描最近 {cli.days} 天的 TradeWinds 新闻...", flush=True)
    rows = fetch_tradewinds_candidates(cli.days, cli.limit)
    if cli.ids:
        wanted = {int(x) for x in cli.ids.split(",") if x.strip()}
        rows = [r for r in rows if r["id"] in wanted]
    print(f"受影响（低质量正文）: {len(rows)} 条\n", flush=True)
    for r in rows:
        print(f"  id={r['id']} {str(r['date'])[:10]} [{r['reason']}] {(r['title'] or '')[:45]}", flush=True)

    if not cli.execute:
        print("\n预览模式，未做修改。确认后加 --execute 执行。", flush=True)
        return
    if not rows:
        print("没有需要处理的记录。", flush=True)
        return

    summary = asyncio.run(run(rows))
    print("\n========== 汇总 ==========", flush=True)
    print(f"受影响总数: {summary['total']}", flush=True)
    print(f"重新抓取成功: {summary['fetched']}", flush=True)
    print(f"重处理成功: {summary['reprocessed']}", flush=True)
    for nid, err in summary["fetch_failed"].items():
        print(f"  抓取失败 id={nid}: {err}", flush=True)
    for nid, err in summary["reprocess_failed"].items():
        print(f"  处理失败 id={nid}: {err}", flush=True)


if __name__ == "__main__":
    main()

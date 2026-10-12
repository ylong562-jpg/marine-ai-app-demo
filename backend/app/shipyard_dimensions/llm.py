# -*- coding: utf-8 -*-
"""
LLM 调用层：复用 news_engine 的 analyzer 模式。

与 app/news_engine/ai_news_core_content_extraction.py 保持一致：
    analyzer = BailianWebAnalyzer(region="beijing")
    await analyzer.analyze(content=..., prompt=..., max_tokens=..., model="qwen-plus")

key 来源：DASHSCOPE_API_KEY 环境变量；缺失时回退加载 backend/service.env
（与 app/scripts/run_news_crawler.sh 的加载方式一致）。
若 news_engine 导入失败，回退到包内等价的最小 OpenAI 兼容客户端。
"""
import asyncio
import json
import os
import re
import sys
from typing import Any, Optional

from .prompts import CLASSIFICATION_PROMPT, EXPLANATION_PROMPT, SCORING_PROMPT_TEMPLATE

MODEL = "qwen-plus"

_analyzer = None
_analyzer_lock = asyncio.Lock()

_LLM_SEMAPHORE = asyncio.Semaphore(4)


def _ensure_api_key() -> bool:
    if os.getenv("DASHSCOPE_API_KEY"):
        return True
    backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env_path = os.path.join(backend_root, "service.env")
    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            # 无 dotenv 时手动解析 KEY=VALUE 行
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except OSError:
                pass
    return bool(os.getenv("DASHSCOPE_API_KEY"))


def _get_analyzer():
    global _analyzer
    if _analyzer is not None:
        return _analyzer
    news_engine_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news_engine"
    )
    if news_engine_dir not in sys.path:
        sys.path.insert(0, news_engine_dir)
    try:
        from ai_engine import BailianWebAnalyzer  # type: ignore
        _analyzer = BailianWebAnalyzer(region="beijing")
    except Exception as e:  # noqa: BLE001
        print(f"[shipyard_dimensions] news_engine analyzer 导入失败，使用内置客户端: {e}")
        _analyzer = _FallbackAnalyzer()
    return _analyzer


class _FallbackAnalyzer:
    """与 BailianWebAnalyzer 等价的最小实现（同 base_url / key / 接口）。"""

    def __init__(self):
        from openai import OpenAI
        api_key = os.getenv("DASHSCOPE_API_KEY")
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    async def analyze(self, url=None, content=None, prompt="", max_tokens=2000, model=MODEL, semaphore=None):
        async def _call():
            try:
                resp = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": content or url or ""},
                    ],
                    max_tokens=max_tokens,
                    temperature=0.1,
                )
                return {"success": True, "analysis": resp.choices[0].message.content or ""}
            except Exception as e:  # noqa: BLE001
                return {"success": False, "error": str(e)}

        if semaphore:
            async with semaphore:
                return await _call()
        return await _call()


async def _analyze(content: str, prompt: str, max_tokens: int = 2000) -> Optional[str]:
    if not _ensure_api_key():
        print("[shipyard_dimensions] 未配置 DASHSCOPE_API_KEY，跳过 LLM 调用")
        return None
    analyzer = _get_analyzer()
    async with _LLM_SEMAPHORE:
        try:
            result = await asyncio.wait_for(
                analyzer.analyze(content=content, prompt=prompt, max_tokens=max_tokens, model=MODEL),
                timeout=180,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[shipyard_dimensions] LLM 调用异常: {e}")
            return None
    if not result or not result.get("success"):
        err = (result or {}).get("error", "未知错误")
        print(f"[shipyard_dimensions] LLM 调用失败: {err}")
        return None
    return result.get("analysis")


def extract_json_from_text(text: Any) -> Optional[Any]:
    if text is None:
        return None
    if isinstance(text, (dict, list)):
        return text
    s = str(text).strip()
    if not s:
        return None
    # 去掉 markdown 代码围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        pass
    # 从文本中提取第一个 JSON 对象/数组
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        start = s.find(start_ch)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == start_ch:
                depth += 1
            elif ch == end_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start : i + 1])
                    except (json.JSONDecodeError, TypeError):
                        break
    return None


def _news_content(news: dict) -> str:
    parts = [f"标题：{news.get('title') or ''}"]
    summary = (news.get("summary") or "").strip()
    # 去掉系统注入的日期前缀，截断控制 token
    summary = re.sub(r"【该新闻的(?:日期|发布日期)是[^】]*】\s*", "", summary)
    parts.append(f"正文：{summary[:3000]}")
    return "\n".join(parts)


async def classify_news(news: dict, name_variants: list[str]) -> Optional[dict]:
    """返回 {"related": bool, "categories": [...], "is_promo": bool} 或 None（调用失败）。"""
    names = " / ".join(name_variants[:6])
    content = (
        f"目标船厂名称（含变体）：{names}\n"
        f"来源平台：{news.get('source_platform') or 'unknown'}\n"
        f"日期：{news.get('date') or ''}\n"
        + _news_content(news)
    )
    raw = await _analyze(content, CLASSIFICATION_PROMPT, max_tokens=600)
    data = extract_json_from_text(raw)
    if not isinstance(data, dict):
        return None
    categories = data.get("categories") or []
    if not isinstance(categories, list):
        categories = [categories]
    return {
        "related": bool(data.get("related")),
        "categories": [str(c) for c in categories],
        "is_promo": bool(data.get("is_promo")),
    }


async def score_evidence(news: dict, shipyard_name: str, category: str) -> Optional[int]:
    """单条证据在指定类别下打 1~100 分；失败返回 None。"""
    prompt = SCORING_PROMPT_TEMPLATE.format(
        shipyard=shipyard_name,
        category=category,
        source_platform=news.get("source_platform") or "unknown",
        date=news.get("date") or "",
        title=news.get("title") or "",
        content="（见下方正文）",
    )
    raw = await _analyze(_news_content(news), prompt, max_tokens=400)
    data = extract_json_from_text(raw)
    if isinstance(data, dict):
        score = data.get("score")
    else:
        m = re.search(r"\b(\d{1,3})\b", str(raw or ""))
        score = int(m.group(1)) if m else None
    try:
        score = int(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if score < 1 or score > 100:
        score = max(1, min(100, score))
    return score


async def score_evidence_batch(
    news: dict, shipyard_name: str, categories: list[str]
) -> dict[str, int]:
    """
    一次 LLM 调用对一条新闻的多个类别分别打分（省调用量）。
    提示词 = 打分提示词（取第一个类别完整渲染）+ 多类别输出格式要求。
    返回 {category: score}；解析失败的类别不出现在结果中。
    """
    base_prompt = SCORING_PROMPT_TEMPLATE.format(
        shipyard=shipyard_name,
        category="、".join(categories),
        source_platform=news.get("source_platform") or "unknown",
        date=news.get("date") or "",
        title=news.get("title") or "",
        content="（见下方正文）",
    )
    multi_prompt = (
        base_prompt
        + "\n\n该新闻同时属于多个证据类别。请为每个类别分别打分，输出合法 JSON："
        + "{"
        + ", ".join(f'"{c}": 1~100 的整数' for c in categories)
        + "}，JSON 外不要输出任何文字。"
    )
    raw = await _analyze(_news_content(news), multi_prompt, max_tokens=120)
    # 注意：content 传的是 news 正文本身已由 prompt 内嵌，这里传标题避免超长
    data = extract_json_from_text(raw)
    scores: dict[str, int] = {}
    if isinstance(data, dict):
        for cat in categories:
            val = data.get(cat)
            try:
                s = int(val)  # type: ignore[arg-type]
                scores[cat] = max(1, min(100, s))
            except (TypeError, ValueError):
                continue
    return scores


async def explain_dimension(
    dimension: str, score: float, top_evidence: list[dict]
) -> Optional[str]:
    lines = []
    for i, ev in enumerate(top_evidence[:3], 1):
        lines.append(
            f"{i}. [{ev.get('category')}] {ev.get('date')}《{ev.get('title')}》"
            f"（证据分 {ev.get('score')}）"
        )
    prompt = EXPLANATION_PROMPT.format(
        dimension=dimension,
        score=score,
        evidence_block="\n".join(lines) or "（无有效证据）",
    )
    raw = await _analyze("\n".join(lines) or "无", prompt, max_tokens=300)
    data = extract_json_from_text(raw)
    if isinstance(data, dict) and data.get("explanation"):
        return str(data["explanation"]).strip()
    if raw:
        text = re.sub(r"```.*?```", "", str(raw), flags=re.DOTALL).strip().strip('"')
        return text[:300] or None
    return None

# -*- coding: utf-8 -*-
"""
扩展 pmc_builder_map：将 clarkson_fleet 中的 builder 名称对齐到 shipyard_data。

背景：clarkson_fleet（86675 行，4160 个 distinct builder）经现有 pmc_builder_map
（209 条）只能命中约 24%。本脚本对未映射 builder 分层匹配：

  1. auto_exact   — 规范化后与船厂英文名/中文名/缩写/曾用名/现有映射精确相等
  2. auto_contains— 规范化后一方包含另一方（如 "HD Hyundai Mipo" ⊃ "Hyundai Mipo"）
  3. llm          — 剩余高频 builder 交 LLM 从词法相近候选中选择（可拒绝）

默认 dry-run（只打印不匹配审计表）；--apply 写入 pmc_builder_map，
match_type 标记来源（fleet_auto_exact / fleet_auto_contains / fleet_llm），
便于追溯与回滚（DELETE FROM pmc_builder_map WHERE match_type LIKE 'fleet_%'）。

用法（backend 目录）：
    .venv/bin/python -m app.scripts.extend_builder_map            # dry-run
    .venv/bin/python -m app.scripts.extend_builder_map --apply   # 写入
    .venv/bin/python -m app.scripts.extend_builder_map --apply --min-ships 5
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

import mysql.connector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.config import get_settings  # noqa: E402

COLLATE = "COLLATE utf8mb4_unicode_ci"

# 规范化：小写、去标点、压缩空格（保留括号内容——它是船厂区分信息）
_PUNCT_RE = re.compile(r"[^\w\s()一-鿿-]")


def norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.replace("（", "(").replace("）", ")")
    s = _PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def core_name(s: str) -> str:
    """去掉公司后缀与括号内容后的核心名（用于 contains 匹配）。"""
    s = norm(s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(
        r"(?i)\b(co\.?|ltd\.?|limited|corporation|corp\.?|inc\.?|llc|s\.a\.?|sa|bv|b\.v\.?|gmbh|kk|co ltd)\b",
        " ",
        s,
    )
    return re.sub(r"\s+", " ", s).strip(" .")


# 地理词表：罗马拼音 + 中文（用于一致性校验，跨地名冲突即拒绝）
GEO_TOKENS = set("""
jiangsu zhejiang shandong liaoning guangdong fujian jiangxi hubei anhui hebei heilongjiang
jilin shanxi shaanxi sichuan hunan hainan guangxi yunnan guizhou gansu qinghai ningxia
xinjiang tibet shanghai beijing tianjin chongqing dalian qingdao ningbo xiamen shenzhen
guangzhou zhuhai shantou fuzhou quanzhou zhangzhou wenzhou taizhou zhoushan nantong
yangzhou lianyungang nanjing rizhao weihai yantai qidong jiangyin jingjiang rugao haimen
dongying yizheng wuhan wuchang nanning beihai fangchenggang huludao yingkou jinzhou panjin
rongcheng xiangshan ninghai ruian yueqing yancheng baoding changzhou wuhu maanshan
""".split())
GEO_ZH = ("上海 北京 天津 重庆 江苏 浙江 山东 辽宁 广东 福建 江西 湖北 安徽 河北 黑龙江 吉林 "
          "山西 陕西 四川 湖南 海南 广西 云南 贵州 甘肃 青海 宁夏 新疆 西藏 大连 青岛 宁波 "
          "厦门 深圳 广州 珠海 汕头 福州 泉州 漳州 温州 台州 舟山 南通 扬州 连云港 南京 "
          "日照 威海 烟台 启东 江阴 靖江 如皋 海门 东营 仪征 武汉 武昌 南宁 北海 防城港 "
          "葫芦岛 营口 锦州 盘锦 荣成 象山 宁海 瑞安 乐清 盐城 保定 常州 芜湖 马鞍山").split()


def geo_tokens(s: str) -> set:
    s = norm(s)
    toks = {t for t in GEO_TOKENS if re.search(rf"\b{re.escape(t)}\b", s)}
    for zh in GEO_ZH:
        if zh in s:
            toks.add(zh)
    return toks


def _token_contained(shorter: str, longer: str) -> bool:
    """shorter 的分词序列必须作为连续词序列出现在 longer 中（词边界，非单词内部子串）。"""
    st, lt = shorter.split(), longer.split()
    if not st or len(st) > len(lt):
        return False
    for i in range(len(lt) - len(st) + 1):
        if lt[i : i + len(st)] == st:
            return True
    return False


# 已知更名/集团关系的手工映射（Clarkson builder -> shipyard_data.id）
MANUAL_MAP = {
    "daewoo (dsme)": 500,  # DSME 大宇造船 -> 韩华海洋(合并条目含 Hanwha Ocean)
    "hd hyundai mipo": 362,  # 现代尾浦
    "jiangsu yangzijiang": 77,  # 扬子江船业集团
    "mhi nagasaki": 285,  # 三菱重工长崎
    "mhi shimonoseki": 285,  # 三菱重工下关
    "jmu tsu shipyard": 334,  # 日本造船联合(JMU)津
    "jmu kure shipyard": 334,  # JMU 吴
    "jmu ariake shipyard": 334,  # JMU 有明
    "gsi liwan": 58,  # 广船国际(荔湾)
    "beihai shipyard": 48,  # 青岛北海造船
    "jiangsu runyang": 457,  # 江苏润扬船业
    "stx sb (jinhae)": 402,  # STX镇海 -> K Shipbuilding
}

# 明确不映射（通用集团名/弱证据匹配，避免污染统计）
SKIP_BUILDERS = {"damen shipyards", "scs shipbuilding"}


def main():
    parser = argparse.ArgumentParser(description="扩展 pmc_builder_map（clarkson_fleet 对齐）")
    parser.add_argument("--apply", action="store_true", help="写入数据库（默认 dry-run）")
    parser.add_argument("--min-ships", type=int, default=3, help="纳入匹配的船数下限（默认3）")
    parser.add_argument("--min-year", type=int, default=2010, help="只看该年份及以后的建造记录（默认2010）")
    parser.add_argument("--llm-top", type=int, default=80, help="交 LLM 判定的 builder 数量上限（按船数排序）")
    args = parser.parse_args()

    conn = mysql.connector.connect(**get_settings().NEWS_DB_CONFIG)
    cur = conn.cursor(dictionary=True)

    # 1) 现有映射 + 船厂名
    cur.execute("SELECT builder_raw, shipyard_id, shipyard_en FROM pmc_builder_map")
    existing = {norm(r["builder_raw"]): r for r in cur.fetchall()}
    cur.execute(
        "SELECT id, English_name, Local_name, Abreviation, former_name FROM shipyard_data"
    )
    yards = cur.fetchall()

    yard_names: dict[int, set] = defaultdict(set)  # yard_id -> normalized names
    name_to_yard: dict[str, int] = {}
    for y in yards:
        for key in ("English_name", "Local_name", "Abreviation", "former_name"):
            v = (y.get(key) or "").strip()
            if not v:
                continue
            for form in (v, core_name(v)):
                if len(form) < 4:
                    continue
                yard_names[y["id"]].add(form)
                name_to_yard.setdefault(form, y["id"])

    # 2) 未映射的 fleet builder（按船数排序）
    cur.execute(
        f"""
        SELECT f.builder, COUNT(*) ships, MAX(f.built) latest, MIN(f.built) earliest
        FROM clarkson_fleet f
        LEFT JOIN pmc_builder_map m ON f.builder {COLLATE} = m.builder_raw
        WHERE f.built >= %s AND f.builder IS NOT NULL AND f.builder != ''
          AND m.shipyard_id IS NULL
        GROUP BY f.builder
        HAVING ships >= %s
        ORDER BY ships DESC
        """,
        (args.min_year, args.min_ships),
    )
    unmapped = cur.fetchall()
    print(f"未映射 builder（{args.min_year}+, ≥{args.min_ships}艘）: {len(unmapped)} 个")

    # 3) 分层匹配
    auto_exact: list[dict] = []
    auto_contains: list[dict] = []
    to_llm: list[dict] = []
    skipped = 0
    for row in unmapped:
        b = row["builder"]
        nb = norm(b)
        ncore = core_name(b)
        if not nb or nb in ("unknown yard", "unknown", "various", "n/a"):
            skipped += 1
            continue
        if nb in SKIP_BUILDERS:
            skipped += 1
            continue
        # 3.0 手工映射（更名/集团关系）
        if nb in MANUAL_MAP:
            auto_exact.append({**row, "shipyard_id": MANUAL_MAP[nb], "match_type": "fleet_manual"})
            continue
        hit = None
        # 3a 精确
        for form in (nb, ncore):
            if form and form in name_to_yard:
                hit = (name_to_yard[form], "auto_exact")
                break
            if form and form in existing:
                hit = (existing[form]["shipyard_id"], "auto_exact")
                break
        # 3b 包含匹配（收紧规则，见下）
        if hit is None and len(ncore) >= 6:
            b_geo = geo_tokens(b)
            for yid, names in yard_names.items():
                # 地理一致性：builder 含地名且船厂名也含地名时必须一致
                y_all_geo: set = set()
                for yn in names:
                    y_all_geo |= geo_tokens(yn)
                if b_geo and y_all_geo and not (b_geo & y_all_geo):
                    continue
                for yn in names:
                    yn_core = core_name(yn)
                    if len(yn_core) < 4:
                        continue
                    shorter, longer = (
                        (ncore, yn_core) if len(ncore) <= len(yn_core) else (yn_core, ncore)
                    )
                    if shorter not in longer:
                        continue
                    # 词边界：按空格分词，禁止单词内部子串（shenglian ⊃ hengli 之误）
                    if not _token_contained(shorter, longer):
                        continue
                    # 单侧为单词时：该词不得是纯地理词（rizhao/ruian 等误判来源）
                    if len(shorter.split()) == 1 and geo_tokens(shorter):
                        continue
                    # 两侧都是多词时，短侧至少 2 个词（防止通用词根单 token 误配）
                    if len(shorter.split()) < 2:
                        continue
                    hit = (yid, "auto_contains")
                    break
                if hit:
                    break
        if hit:
            (auto_exact if hit[1] in ("auto_exact", "fleet_manual") else auto_contains).append(
                {**row, "shipyard_id": hit[0], "match_type": hit[1]}
            )
        else:
            to_llm.append(row)

    print(f"auto_exact: {len(auto_exact)}  auto_contains: {len(auto_contains)}  待LLM: {len(to_llm)}  跳过(Unknown等): {skipped}")

    # 4) LLM 判定剩余高频 builder
    llm_results: list[dict] = []
    if to_llm:
        try:
            llm_results = _llm_resolve(to_llm[: args.llm_top], yards)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ LLM 判定不可用（{e}），跳过该层", file=sys.stderr)

    # 5) 审计输出
    def _yard_name(yid):
        for y in yards:
            if y["id"] == yid:
                return y["English_name"] or y["Local_name"]
        return str(yid)

    print("\n===== 自动匹配（精确）=====")
    for r in auto_exact:
        print(f"  {r['builder']}  [{r['ships']}艘 {r['earliest']}-{r['latest']}]  ->  {_yard_name(r['shipyard_id'])} (id={r['shipyard_id']})")
    print("\n===== 自动匹配（包含）=====")
    for r in auto_contains:
        print(f"  {r['builder']}  [{r['ships']}艘 {r['earliest']}-{r['latest']}]  ->  {_yard_name(r['shipyard_id'])} (id={r['shipyard_id']})")
    print("\n===== LLM 匹配 =====")
    for r in llm_results:
        tag = f"-> {_yard_name(r['shipyard_id'])} (id={r['shipyard_id']})" if r.get("shipyard_id") else "-> 无匹配"
        print(f"  {r['builder']}  [{r.get('ships','?')}艘]  {tag}")
    unresolved = [r for r in to_llm[args.llm_top:]] + [r for r in to_llm[: args.llm_top] if not any(x["builder"] == r["builder"] and x.get("shipyard_id") for x in llm_results)]
    print(f"\n未解决 builder: {len(unresolved)} 个（保留 NULL，不影响其他行统计）")

    # 6) 写入
    new_rows = auto_exact + auto_contains + [r for r in llm_results if r.get("shipyard_id")]
    if not args.apply:
        print(f"\n[dry-run] 可新增映射 {len(new_rows)} 条；加 --apply 写入")
        return
    inserted = 0
    for r in new_rows:
        try:
            cur.execute(
                """
                INSERT INTO pmc_builder_map (builder_raw, shipyard_id, shipyard_en, shipyard_zh, match_type, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE shipyard_id = VALUES(shipyard_id)
                """,
                (
                    r["builder"],
                    int(r["shipyard_id"]),
                    _yard_name(r["shipyard_id"]),
                    "",
                    r.get("match_type", "fleet_auto"),
                ),
            )
            inserted += 1
        except mysql.connector.Error as e:
            print(f"  ❌ 写入失败 {r['builder']}: {e}")
    conn.commit()
    print(f"\n✅ 已写入 {inserted} 条映射")

    # 7) 写入后覆盖率
    cur.execute(
        f"""
        SELECT COUNT(*) total, SUM(m.shipyard_id IS NOT NULL) mapped
        FROM clarkson_fleet f
        LEFT JOIN pmc_builder_map m ON f.builder {COLLATE} = m.builder_raw
        WHERE f.built >= %s
        """,
        (args.min_year,),
    )
    r = cur.fetchone()
    print(f"覆盖率（{args.min_year}+）: {r['mapped']}/{r['total']} = {float(r['mapped'])/r['total']*100:.1f}%")
    conn.close()


def _llm_resolve(rows: list[dict], yards: list[dict]) -> list[dict]:
    """LLM 从词法相近候选中为每个 builder 选择船厂（可拒绝）。"""
    from openai import OpenAI

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")
    client = OpenAI(api_key=api_key, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

    # 每个 builder 的词法候选（token 交集 top5）
    def tokens(s: str) -> set:
        return set(re.findall(r"[a-z一-鿿0-9]+", norm(s)))

    yard_tokens = [(y, tokens(" ".join(str(y.get(k) or "") for k in ("English_name", "Local_name", "former_name")))) for y in yards]

    results = []
    for row in rows:
        bt = tokens(row["builder"])
        scored = []
        for y, yt in yard_tokens:
            if not bt or not yt:
                continue
            j = len(bt & yt) / len(bt | yt)
            if j > 0:
                scored.append((j, y))
        scored.sort(key=lambda x: -x[0])
        cands = scored[:5]
        if not cands:
            results.append({**row, "shipyard_id": None})
            continue
        cand_txt = "\n".join(
            f"{i+1}. id={y['id']}  {y['English_name'] or ''} / {y['Local_name'] or ''}"
            for i, (_, y) in enumerate(cands)
        )
        prompt = (
            "你是造船行业数据专家。把 Clarkson 船厂名对齐到内部船厂档案，只考虑名称指代同一造船实体（同一船厂/同一基地）。"
            "注意集团关系：HD Hyundai Mipo=Hyundai Mipo Dockyard；Daewoo(DSME)=Hanwha Ocean(玉浦)；"
            "HD Hyundai Vietnam=Hyundai Vietnam Shipyard。\n\n"
            f"Clarkson builder: \"{row['builder']}\"\n候选:\n{cand_txt}\n\n"
            "只输出JSON: {\"choice\": 候选序号或0, \"confidence\": \"high|low\"}。0=都不匹配；"
            "只有名称明确指同一实体且confidence=high时才给候选序号，否则0。"
        )
        resp = client.chat.completions.create(
            model="qwen-plus",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=60,
            temperature=0,
        )
        txt = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        choice, conf = 0, "low"
        if m:
            try:
                d = json.loads(m.group(0))
                choice = int(d.get("choice") or 0)
                conf = str(d.get("confidence") or "low")
            except (json.JSONDecodeError, ValueError):
                pass
        if 1 <= choice <= len(cands) and conf == "high":
            results.append({**row, "shipyard_id": cands[choice - 1][1]["id"], "match_type": "fleet_llm"})
        else:
            results.append({**row, "shipyard_id": None})
    return results


if __name__ == "__main__":
    main()

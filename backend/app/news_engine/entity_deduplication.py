# -*- coding: utf-8 -*-
"""
实体归一化/查重模块（大模型匹配版）

功能：
1. 为每种实体类型维护一个标准名称表（canonical names，来自
   /path/to/normalized_entity_vocabulary.json），并标记每个实体的
   宽泛/具体属性（specificity: broad/specific）。
2. 当新新闻提取到某个实体时：
   - 先进行精确/别名匹配；
   - 再进行子串包含匹配；
   - 仍无命中时，调用大模型判断是否与已有标准名指向同一主体；
   - 命中则替换为标准名；未命中时【具体实体】自动注册进词表（恢复旧导入
     机制，下次自愈），【宽泛实体】不注册、原样返回。
   - 宽泛实体必须遵守“地区+分类”或“分类”的命名规则；具体实体不能被归一化到宽泛标准名。
3. 不使用 difflib/编辑距离等模糊匹配作为最终决策依据，仅用于从大表中快速召回候选。

数据存储：/path/to/normalized_entity_vocabulary.json（JSON 词表，支持具体实体自动注册）
条目格式: {canonical_name, entity_types[], english_name, aliases[], frequency, code}
"""

import os
import json
import re
import fcntl
import difflib
import asyncio


# 实体词表：统一使用规范词表 JSON（只读，可用环境变量覆盖）。
ENTITY_VOCAB_PATH = os.getenv(
    "ENTITY_VOCABULARY_PATH",
    "/path/to/normalized_entity_vocabulary.json",
)

# ==================== 宽泛/具体实体分类规则 ====================

# 地区词（按长度降序排列，优先匹配最长）
_REGION_WORDS = sorted({
    "中国", "国内", "国外", "海外", "国际", "欧洲", "亚洲", "美洲", "非洲", "大洋洲",
    "东南亚", "东亚", "南亚", "西亚", "西欧", "北欧", "南欧", "东欧", "中东",
    "南美", "北美", "拉丁美洲", "中亚", "东北亚", "澳新", "欧亚", "环太平洋",
    "境内", "境外",
    "新加坡", "韩国", "日本", "德国", "希腊", "土耳其", "印尼", "印度尼西亚",
    "越南", "丹麦", "瑞典", "英国", "美国", "阿联酋", "阿布扎比", "卡塔尔",
    "巴西", "意大利", "法国", "荷兰", "挪威", "西班牙", "印度", "菲律宾",
    "俄罗斯", "乌克兰", "比利时", "加拿大", "澳大利亚", "新西兰", "马来西亚",
    "泰国", "缅甸", "孟加拉", "巴基斯坦", "伊朗", "以色列", "埃及", "南非",
    "尼日利亚", "墨西哥", "阿根廷", "智利", "秘鲁", "哥伦比亚", "委内瑞拉",
    "波兰", "捷克", "匈牙利", "罗马尼亚", "保加利亚", "克罗地亚", "塞尔维亚",
    "葡萄牙", "爱尔兰", "瑞士", "奥地利", "芬兰", "冰岛", "卢森堡", "斯洛伐克",
    "斯洛文尼亚", "爱沙尼亚", "拉脱维亚", "立陶宛", "白俄罗斯", "哈萨克斯坦",
    "乌兹别克斯坦", "土库曼斯坦", "吉尔吉斯斯坦", "塔吉克斯坦", "蒙古", "朝鲜",
    "尼泊尔", "不丹", "斯里兰卡", "马尔代夫", "柬埔寨", "老挝", "文莱", "东帝汶",
    "巴布亚新几内亚", "斐济", "萨摩亚", "汤加", "瓦努阿图", "密克罗尼西亚",
    "帕劳", "基里巴斯", "瑙鲁", "图瓦卢", "所罗门群岛", "马绍尔群岛",
}, key=len, reverse=True)

# 按 entity_type 的分类词核心
_CATEGORY_WORDS = {
    "shipowner": {"船东", "船公司", "航运公司", "船主", "订船方", "订造方", "船东公司", "航运企业", "船东企业", "船东集团"},
    "shipyard": {"船厂", "造船厂", "船企", "造船企业", "造船方", "承建方", "修造船厂", "船舶企业", "造船集团", "船厂企业"},
    "buyer": {"买方", "买家", "购买方", "买主", "买船方", "收购方"},
    "seller": {"卖方", "卖家", "出售方", "卖主", "卖船方", "转让方", "船东"},
    "charterer": {"承租方", "租船人", "承租人", "租家", "租船方", "承租公司", "客户"},
    "owner": {"出租方", "船东", "船主", "船舶所有人", "所有者"},
    "company": {"公司", "企业", "集团"},
    "parties": {"参与方", "各方", "当事方", "相关方", "利益方", "主体", "金融租赁公司", "融资租赁公司", "金租公司", "承租人", "航运公司"},
}

# 宽泛指示词：出现即视为宽泛
_BROAD_INDICATORS = [
    "某", "等", "有关", "相关", "旗下", "各", "一家", "两家", "三家", "四家", "五家",
    "多家", "某家", "未具名", "泛指", "国外某", "国外一家", "国外两家",
    "某国际", "某欧洲", "某亚洲", "某中国", "某国内", "某国外",
]

# 通用修饰词：出现在分类词前面且没有具体名词时，视为宽泛
_GENERIC_MODIFIERS = {
    "非经营性", "经营性", "大型", "中型", "小型", "民营", "国有", "外资", "合资", "独资",
    "专业", "国际", "国内", "传统", "新", "老", "私人", "家族", "上市", "非上市",
    "主要", "次要", "重大", "一般", "特定", "专门", "综合", "单一", "多元",
    "知名", "著名", "领军", "龙头", "大型", "中小型", "大中型", "小微", "头部",
    "本土", "本地", "外地", "海外", "境外", "境内", "跨国", "全球性", "区域性",
    "纳斯达克上市", "上市", "私人",
}

_ORG_SUFFIXES = ["有限公司", "有限责任公司", "股份有限公司", "股份公司", "无限公司"]


def _strip_org_suffixes(name: str) -> str:
    s = name.strip()
    changed = True
    while changed:
        changed = False
        for suffix in _ORG_SUFFIXES:
            if s.endswith(suffix):
                s = s[: -len(suffix)].strip()
                changed = True
                break
    return s


def _is_all_modifiers(text: str) -> bool:
    """判断文本是否仅由通用修饰词组成（允许组合）。"""
    if not text:
        return True
    if text in _GENERIC_MODIFIERS:
        return True
    remaining = text
    while remaining:
        found = False
        for mod in sorted(_GENERIC_MODIFIERS, key=len, reverse=True):
            if remaining.startswith(mod):
                remaining = remaining[len(mod):].strip()
                found = True
                break
        if not found:
            return False
    return True


def _contains_proper_noun_marker(name: str) -> bool:
    """判断是否含有专有名词标记（英文/数字、括号内专有名词等）。"""
    # 括号内有英文或数字
    if re.search(r"[（(][^）)]*[A-Za-z0-9][^）)]*[）)]", name):
        return True
    core = re.sub(r"[（(].*?[）)]", "", name).strip()
    if re.search(r"[A-Za-z0-9]", core):
        return True
    return False


def classify_entity_name(name: str, entity_type: str) -> str:
    """
    判断一个实体名称是宽泛（broad）还是具体（specific）。

    宽泛实体的允许形式：
    - 地区 + 分类（如“中国船厂”、“欧洲船东”）
    - 分类（如“船东”、“买家”）
    - 地区/通用修饰词 + 分类（如“欧洲知名船东”、“非经营性船东”）

    具体实体：具有可识别专有名称的组织/企业/个人（如“南通润邦海洋工程装备有限公司”）。
    """
    if not name or not isinstance(name, str):
        return "specific"

    name = name.strip()
    if not name:
        return "specific"

    # 含有英文/数字等专有名词标记 → 具体（保守）
    if _contains_proper_noun_marker(name):
        return "specific"

    # 宽泛指示词
    for ind in _BROAD_INDICATORS:
        if ind in name:
            return "broad"

    # 去掉括号注释和组织后缀
    core = re.sub(r"[（(].*?[）)]", "", name).strip()
    core = _strip_org_suffixes(core)

    # 为“地区 + 分类词”检测使用所有已知分类词，避免实体类型标注错误导致
    # 类似“国外船东”被存为 shipyard 时漏判为具体实体。
    cats = set()
    for words in _CATEGORY_WORDS.values():
        cats |= words

    # 完全等于分类词
    if core in cats:
        return "broad"

    # 地区 + （可选“籍”） + 分类词
    for region in _REGION_WORDS:
        if core.startswith(region):
            remainder = core[len(region):].strip()
            if remainder.startswith("籍"):
                remainder = remainder[1:].strip()
            if remainder in cats:
                return "broad"
            for cat in sorted(cats, key=len, reverse=True):
                if remainder.endswith(cat):
                    prefix = remainder[: -len(cat)].strip()
                    if _is_all_modifiers(prefix):
                        return "broad"
            break

    # 通用修饰词 + 分类词
    for cat in sorted(cats, key=len, reverse=True):
        if core.endswith(cat):
            prefix = core[: -len(cat)].strip()
            if prefix and _is_all_modifiers(prefix):
                return "broad"

    return "specific"


class EntityRegistry:
    """实体注册表，支持按类型归一化和查重"""

    def __init__(self, db_path=None):
        # db_path 参数仅保留兼容（词表已改为 JSON，自动注册时直接读写该文件）。
        self.db_path = db_path or ENTITY_VOCAB_PATH
        self._analyzer = None
        self._llm_semaphore = asyncio.Semaphore(5)
        self._vocab_loaded = False
        self._entities_by_type = {}
        # 自动注册（旧导入机制）并发保护：进程内 asyncio 锁 + 跨进程文件锁
        self._register_lock = asyncio.Lock()
        self._register_attempted = set()  # 本会话已尝试注册过的 (entity_type, raw_lower)

    def _analyzer_instance(self):
        if self._analyzer is None:
            from ai_engine import BailianWebAnalyzer
            self._analyzer = BailianWebAnalyzer(region="beijing")
        return self._analyzer

    def _load_vocab(self):
        """从实体词表 JSON 加载全部条目，并按 entity_type 展开索引（只读，只做一次）。"""
        if self._vocab_loaded:
            return
        try:
            with open(ENTITY_VOCAB_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"   ⚠️ 实体词表加载失败 [{ENTITY_VOCAB_PATH}]: {e}")
            self._vocab_loaded = True
            return
        items = data if isinstance(data, list) else data.get("entries", [])
        for item in items:
            canonical = str(item.get("canonical_name") or "").strip()
            if not canonical:
                continue
            aliases = item.get("aliases") or []
            if isinstance(aliases, str):
                try:
                    aliases = json.loads(aliases)
                except (json.JSONDecodeError, TypeError):
                    aliases = []
            aliases = [str(a).strip() for a in aliases if str(a).strip()]
            # 新词表一个条目可属于多个类型（entity_types 列表）
            entity_types = item.get("entity_types") or [item.get("entity_type")] or []
            for et in entity_types:
                et = str(et or "").strip()
                if not et:
                    continue
                self._entities_by_type.setdefault(et, []).append({
                    "canonical_name": canonical,
                    "aliases": aliases,
                    "frequency": item.get("frequency") or 1,
                    "specificity": classify_entity_name(canonical, et),
                })
        self._vocab_loaded = True

    def _load_entities_by_type(self, entity_type):
        """加载某类型的所有标准名称（含宽泛/具体标记），来自 JSON 词表。"""
        self._load_vocab()
        return self._entities_by_type.get(entity_type, [])

    @staticmethod
    def _parse_alias_list(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return []
        return [str(a).strip() for a in (value or []) if str(a).strip()]

    def _append_to_memory_index(self, entity_type, canonical, aliases):
        """把新注册实体加入内存索引，供本进程后续匹配立即命中。"""
        self._entities_by_type.setdefault(entity_type, []).append({
            "canonical_name": canonical,
            "aliases": aliases,
            "frequency": 1,
            "specificity": classify_entity_name(canonical, entity_type),
        })

    async def _register_new_entity(self, entity_type, raw):
        """
        未命中的具体实体自动注册进词表 JSON（恢复旧导入机制，下次自愈）。

        规则：
        - 宽泛实体（classify_entity_name == "broad"）不注册，原样返回；
        - 进程内 set + asyncio.Lock 去重，跨进程用 fcntl 文件锁保护读-改-写；
        - 写盘前重新加载词表检查，避免多进程并发注册重复条目；
        - 临时文件 + os.replace 原子替换，避免写坏词表。
        """
        raw = raw.strip()
        key = (entity_type, raw.lower())
        if key in self._register_attempted:
            return
        self._register_attempted.add(key)

        async with self._register_lock:
            # 1. 内存索引再查一次（本进程其他任务可能刚注册过）
            for ent in self._entities_by_type.get(entity_type, []):
                if raw == ent["canonical_name"] or raw in ent["aliases"]:
                    return

            lock_path = str(ENTITY_VOCAB_PATH) + ".lock"
            lock_fp = open(lock_path, "w", encoding="utf-8")
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)

                # 2. 重新加载磁盘词表，跨进程去重
                with open(ENTITY_VOCAB_PATH, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                disk_items = disk if isinstance(disk, list) else disk.get("entries", [])
                for item in disk_items:
                    canonical = str(item.get("canonical_name") or "").strip()
                    aliases = self._parse_alias_list(item.get("aliases"))
                    if raw == canonical or raw in aliases:
                        # 磁盘已有：仅补进内存索引（若该条目含本类型则加入对应类型列表）
                        types = item.get("entity_types") or [item.get("entity_type")] or []
                        if entity_type in types:
                            self._append_to_memory_index(entity_type, canonical, aliases)
                        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                        return

                # 3. 追加新条目并原子写回
                entry = {
                    "canonical_name": raw,
                    "entity_types": [entity_type],
                    "english_name": None,
                    "aliases": [raw],
                    "frequency": 1,
                    "code": None,
                }
                disk_items.append(entry)
                tmp_path = str(ENTITY_VOCAB_PATH) + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(disk if isinstance(disk, list) else {"entries": disk_items},
                              f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(ENTITY_VOCAB_PATH))

                # 4. 更新内存索引，本进程后续立即命中
                self._append_to_memory_index(entity_type, raw, [raw])
                print(f"   ➕ 实体自动注册: [{entity_type}] {raw}")
            finally:
                try:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                lock_fp.close()

    def _exact_or_alias_match(self, raw, entities):
        """精确匹配 canonical 或 alias，返回 (canonical_name, specificity)。"""
        for ent in entities:
            if raw == ent["canonical_name"]:
                return ent["canonical_name"], ent.get("specificity", "specific")
            for alias in ent["aliases"]:
                if raw == alias:
                    return ent["canonical_name"], ent.get("specificity", "specific")
        return None, None

    def _substring_match(self, raw, entities):
        """强子串匹配：raw 被 canonical 包含，或 canonical 被 raw 包含。返回 (canonical_name, specificity)。"""
        best = None
        best_score = 0
        for ent in entities:
            canonical = ent["canonical_name"]
            # raw 被 canonical 包含（如 "外高桥" 在 "外高桥造船" 中）
            if raw in canonical and len(raw) >= 3:
                score = 90 + len(raw) / len(canonical) * 10
                if score > best_score:
                    best_score = score
                    best = (canonical, ent.get("specificity", "specific"))
            # canonical 被 raw 包含
            elif canonical in raw and len(canonical) >= 3:
                score = 80 + len(canonical) / len(raw) * 10
                if score > best_score:
                    best_score = score
                    best = (canonical, ent.get("specificity", "specific"))
        return best

    def _recall_candidates(self, raw, entities, top_k=5, min_ratio=0.35):
        """
        使用 difflib 仅做候选召回，不做最终决策。
        返回按相似度排序的候选标准名（含宽泛/具体标记）列表。
        """
        raw_lower = raw.lower()
        scored = []
        for ent in entities:
            canonical = ent["canonical_name"]
            ratio = difflib.SequenceMatcher(None, raw_lower, canonical.lower()).ratio()
            if ratio >= min_ratio:
                scored.append((ratio, canonical))
            for alias in ent["aliases"]:
                ratio = difflib.SequenceMatcher(None, raw_lower, alias.lower()).ratio()
                if ratio >= min_ratio:
                    scored.append((ratio, canonical))

        scored.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        candidates = []
        for _, name in scored:
            if name not in seen:
                seen.add(name)
                candidates.append(name)
            if len(candidates) >= top_k:
                break
        return candidates

    async def _llm_match(self, entity_type, raw_name, candidates):
        """
        调用大模型判断 raw_name 是否与候选标准名中的某一个指向同一主体。
        返回命中的 canonical_name，未命中返回 None。
        """
        if not candidates:
            return None

        analyzer = self._analyzer_instance()
        candidates_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))

        # 如果新名称是具体实体，过滤掉候选中的宽泛标准名，避免把具体实体误归到宽泛条目
        raw_specificity = classify_entity_name(raw_name, entity_type)
        if raw_specificity == "specific":
            filtered_candidates = []
            for c in candidates:
                # 从已加载实体中查 specificity
                spec = "specific"
                for ent in self._load_entities_by_type(entity_type):
                    if ent["canonical_name"] == c:
                        spec = ent.get("specificity", "specific")
                        break
                if spec != "broad":
                    filtered_candidates.append(c)
            candidates = filtered_candidates or candidates

        candidates_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))

        prompt = f"""你是一名航运实体标准化专家。请判断以下“新名称”是否与某个“已有标准名”指向同一主体（同一公司、船厂、船东、组织、港口、船级社等）。

判断规则：
- 忽略大小写、中英文差异、缩写、以及“集团/公司/航运/造船/重工/船业/企业/能源/运输/港口/管理局/船级社”等通用修饰词差异。
- 只有确实指向同一主体时才返回对应标准名；如果都不匹配，必须返回“无”。
- 不要返回相似但不相同的主体名称。
- 【关键】具体实体（如“南通润邦海洋工程装备有限公司”）不能与宽泛实体（如“中国船厂”、“欧洲船东”）相互匹配；如果新名称是具体公司/船厂/船东，而候选标准名只是“地区+分类”或纯分类的宽泛指代，必须返回“无”。

实体类型：{entity_type}
新名称：{raw_name}

候选标准名：
{candidates_text}

请只返回以下 JSON 格式，不要任何解释、前言或 markdown 代码块：
{{"match": "命中标准名 或 无", "reason": "判断理由（15字以内）"}}"""

        async with self._llm_semaphore:
            try:
                result = await analyzer.analyze(
                    content=prompt,
                    prompt="判断新名称与候选标准名是否指向同一主体，返回JSON",
                    max_tokens=100,
                    model="qwen-plus"
                )

                if not result.get("success"):
                    print(f"   ⚠️ 实体LLM匹配失败: {result.get('error', '未知错误')}")
                    return None

                text = result.get("analysis", "").strip()
                # 去除可能的 markdown 代码块
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)

                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    # 尝试用正则提取 JSON
                    match = re.search(r"\{[^}]+\}", text)
                    if not match:
                        return None
                    parsed = json.loads(match.group(0))

                match_name = str(parsed.get("match", "无")).strip()
                if match_name and match_name != "无" and match_name in candidates:
                    print(f"   🤖 LLM实体匹配: [{entity_type}] {raw_name} -> {match_name}")
                    return match_name
                return None

            except Exception as e:
                print(f"   ⚠️ 实体LLM匹配异常: {str(e)[:80]}")
                return None

    # 类型回退：当特定类型未命中时，在保留的词表类型中继续查找
    # 词表仅保留 5 大主标签实体字段：shipowner, shipyard, buyer, seller,
    # charterer, owner, company, parties
    _FALLBACK_TYPES = {
        "shipowner": ["company"],
        "shipyard": ["company"],
        "buyer": ["company", "shipowner"],
        "seller": ["company", "shipowner"],
        "charterer": ["company", "shipowner"],
        "owner": ["company", "shipowner"],
        "company": ["shipowner"],
        "parties": ["company"],
    }

    async def _find_in_type(self, entity_type, raw):
        """在单一类型中查找标准名，返回 (canonical_name, specificity) 或 None。"""
        entities = self._load_entities_by_type(entity_type)
        if not entities:
            return None

        exact_name, exact_spec = self._exact_or_alias_match(raw, entities)
        if exact_name:
            return exact_name, exact_spec

        sub_result = self._substring_match(raw, entities)
        if sub_result:
            return sub_result

        candidates = self._recall_candidates(raw, entities)
        if candidates:
            llm_match = await self._llm_match(entity_type, raw, candidates)
            if llm_match:
                # 查回 specificity
                for ent in entities:
                    if ent["canonical_name"] == llm_match:
                        return llm_match, ent.get("specificity", "specific")
                return llm_match, "specific"
        return None

    async def find_canonical_async(self, entity_type, raw_name):
        """
        查找 raw_name 对应的标准名称及宽泛/具体标记。
        1. 在指定类型中精确/别名/子串/LLM 匹配
        2. 未命中则在相关回退类型中继续查找
        返回 (canonical_name, specificity) 或 None
        """
        if not raw_name or not isinstance(raw_name, str):
            return None

        raw = raw_name.strip()
        if not raw or len(raw) < 2:
            return None

        # 1. 指定类型
        result = await self._find_in_type(entity_type, raw)
        if result:
            return result

        # 2. 回退到相关类型（解决 vocabulary 类型与 prompt key 不完全对齐的问题）
        for fallback_type in self._FALLBACK_TYPES.get(entity_type, []):
            result = await self._find_in_type(fallback_type, raw)
            if result:
                return result

        return None

    async def normalize_name_async(self, entity_type, raw_name):
        """
        将 raw_name 归一化为标准名称。

        归一化规则：
        - 具体实体不能被归一化为宽泛标准名；
        - 宽泛实体也不能被归一化为具体标准名；
        - 未命中时：具体实体自动注册进词表（旧导入机制，下次自愈），
          宽泛实体不注册、原样返回。
        """
        if not raw_name or not isinstance(raw_name, str):
            return raw_name

        raw = raw_name.strip()
        if not raw or len(raw) < 2:
            return raw

        raw_specificity = classify_entity_name(raw, entity_type)

        match = await self.find_canonical_async(entity_type, raw)
        if match:
            canonical, canonical_spec = match
            # 宽泛/具体属性不一致时，不允许归一化
            if raw_specificity != canonical_spec:
                print(f"   ⚠️ 实体属性冲突，跳过归一化: [{entity_type}] {raw} ({raw_specificity}) -> {canonical} ({canonical_spec})")
                # 不注册新的，保留原文
                return raw
            # 词表为只读：只做检测归一化，不更新频率、不添加别名。
            return canonical

        # 未命中：具体实体自动注册进词表（旧导入机制，下次自愈）；宽泛实体原样返回
        if raw_specificity == "specific":
            try:
                await self._register_new_entity(entity_type, raw)
            except Exception as e:
                print(f"   ⚠️ 实体自动注册失败 [{entity_type}] {raw}: {str(e)[:80]}")
        return raw

    async def normalize_entity_json_async(self, entity_json):
        """
        递归处理 entity JSON（字符串或dict/list），将其中的字符串值归一化。
        """
        # 词表仅保留 5 大主标签对应的实体字段：
        # 新造船订单/船舶建造：shipowner, shipyard
        # 买卖船：buyer, seller
        # 租船：charterer, owner
        # 公司金融：company, parties
        KEY_TO_TYPE = {
            "shipowner": "shipowner",
            "shipowners": "shipowner",
            "shipyard": "shipyard",
            "shipyards": "shipyard",
            "buyer": "buyer",
            "buyers": "buyer",
            "seller": "seller",
            "sellers": "seller",
            "charterer": "charterer",
            "charterers": "charterer",
            "owner": "owner",
            "owners": "owner",
            "company": "company",
            "companies": "company",
            "parties": "parties",
            "party": "parties",
        }

        if isinstance(entity_json, str):
            try:
                parsed = json.loads(entity_json)
                normalized = await self.normalize_entity_json_async(parsed)
                return json.dumps(normalized, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                return entity_json

        if isinstance(entity_json, dict):
            result = {}
            for key, value in entity_json.items():
                entity_type = KEY_TO_TYPE.get(key)
                if entity_type is None:
                    # 非词表字段：保留原值，仅递归处理嵌套结构
                    if isinstance(value, dict):
                        result[key] = await self.normalize_entity_json_async(value)
                    elif isinstance(value, list):
                        result[key] = [await self.normalize_entity_json_async(item) for item in value]
                    else:
                        result[key] = value
                    continue
                if isinstance(value, list):
                    result[key] = [
                        await self.normalize_name_async(entity_type, v)
                        if isinstance(v, str) else v
                        for v in value
                    ]
                elif isinstance(value, str):
                    result[key] = await self.normalize_name_async(entity_type, value)
                elif isinstance(value, dict):
                    result[key] = await self.normalize_entity_json_async(value)
                else:
                    result[key] = value
            return result

        if isinstance(entity_json, list):
            return [await self.normalize_entity_json_async(item) for item in entity_json]

        return entity_json


    async def normalize_data_json_async(self, data_json):
        """
        递归处理 data JSON（字符串或 dict/list），对 transactions 中的主体字段做查重归一化。
        支持分组格式 [{"main_tag": ..., "data": ...}] 以及 transactions 数组/平铺字典。
        """
        # 词表仅保留 5 大主标签对应的实体字段：
        # 新造船订单/船舶建造/维修改装：shipowner, shipyard（facility 视为 shipyard）
        # 买卖船：buyer, seller
        # 租船：charterer, owner
        # 公司金融：company, parties
        KEY_TO_TYPE = {
            "shipowner": "shipowner",
            "shipowners": "shipowner",
            "shipyard": "shipyard",
            "shipyards": "shipyard",
            "buyer": "buyer",
            "buyers": "buyer",
            "seller": "seller",
            "sellers": "seller",
            "charterer": "charterer",
            "charterers": "charterer",
            "owner": "owner",
            "owners": "owner",
            "facility": "shipyard",
            "facilities": "shipyard",
            "company": "company",
            "companies": "company",
            "parties": "parties",
            "party": "parties",
        }

        if isinstance(data_json, str):
            try:
                parsed = json.loads(data_json)
                normalized = await self.normalize_data_json_async(parsed)
                return json.dumps(normalized, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                return data_json

        if isinstance(data_json, dict):
            result = {}
            for key, value in data_json.items():
                key_lower = str(key).lower()
                # 若是分组格式中的 data 字段（字符串 JSON），递归处理
                if key_lower == "data" and isinstance(value, str):
                    try:
                        inner = json.loads(value)
                        result[key] = json.dumps(
                            await self.normalize_data_json_async(inner),
                            ensure_ascii=False,
                        )
                    except (json.JSONDecodeError, TypeError):
                        result[key] = value
                # 对 transactions 数组递归处理
                elif key_lower == "transactions" and isinstance(value, list):
                    result[key] = [await self.normalize_data_json_async(item) for item in value]
                elif key_lower in KEY_TO_TYPE and isinstance(value, str):
                    entity_type = KEY_TO_TYPE[key_lower]
                    result[key] = await self.normalize_name_async(entity_type, value)
                elif key_lower in KEY_TO_TYPE and isinstance(value, list):
                    entity_type = KEY_TO_TYPE[key_lower]
                    result[key] = [
                        await self.normalize_name_async(entity_type, v)
                        if isinstance(v, str) else v
                        for v in value
                    ]
                elif isinstance(value, dict):
                    result[key] = await self.normalize_data_json_async(value)
                elif isinstance(value, list):
                    result[key] = [await self.normalize_data_json_async(item) for item in value]
                else:
                    result[key] = value
            return result

        if isinstance(data_json, list):
            return [await self.normalize_data_json_async(item) for item in data_json]

        return data_json


# 全局单例
_registry_instance = None


def get_entity_registry():
    """获取全局实体注册表实例"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = EntityRegistry()
    return _registry_instance


async def deduplicate_entity_async(entity_json):
    """
    对外异步接口：对 entity JSON 进行实体查重/归一化（大模型匹配）。
    """
    registry = get_entity_registry()
    return await registry.normalize_entity_json_async(entity_json)


async def deduplicate_data_async(data_json):
    """
    对外异步接口：对 data JSON 中的 transactions 主体字段进行查重/归一化。
    """
    registry = get_entity_registry()
    return await registry.normalize_data_json_async(data_json)


# 保留旧同步接口（仅推荐在同步独立脚本中使用；异步流程请使用 deduplicate_entity_async）
def deduplicate_entity(entity_json, min_ratio=0.65):
    """
    对外同步接口：对 entity JSON 进行实体查重/归一化。
    注意：在已有事件循环的异步上下文中调用会引发嵌套循环错误，请改用 deduplicate_entity_async。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        raise RuntimeError(
            "deduplicate_entity() 不能在异步上下文中使用，请改用 deduplicate_entity_async()。"
        )

    registry = get_entity_registry()
    return asyncio.run(registry.normalize_entity_json_async(entity_json))


if __name__ == "__main__":
    # 简单测试
    async def _test():
        registry = get_entity_registry()
        await registry.normalize_name_async("shipyard", "外高桥造船")
        await registry.normalize_name_async("shipyard", "外高桥")
        await registry.normalize_name_async("shipowner", "中远海运能源")

        test_entity = {
            "shipowner": "中远海能",
            "shipyard": "外高桥",
            "companies": ["中远海控", "某新公司"]
        }
        print(json.dumps(await deduplicate_entity_async(test_entity), ensure_ascii=False, indent=2))

    asyncio.run(_test())

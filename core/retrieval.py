"""Human-like Memory · 检索。

纯关键词实现，但比「2 字片段 LIKE」聪明：
- 停用词过滤：不拿「的」「了」「我」去搜
- 片段加权：4 字 > 3 字 > 2 字
- 位置加分：出现在句子开头的片段更可能是主题
- 同义词扩展：搜「啰嗦」也能找到「话多」「别太满」
- 结果去重：同一件事的多个说法只留一条

没有向量也能用；如果本机有 Ollama + bge-m3，可以在 store 层叠加上语义召回。
"""
from __future__ import annotations

import re
from typing import Iterable

# ---------- 停用词 ----------
# 这些词几乎出现在每句话里，拿去检索只会把无关记忆全捞上来
STOPWORDS: set[str] = {
    "的", "了", "是", "在", "有", "和", "就", "都", "也", "这", "那", "个",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "自己",
    "什么", "怎么", "为什么", "一个", "这个", "那个", "这样", "那样",
    "但是", "然后", "而且", "所以", "如果", "因为", "虽然", "可以",
    "不", "没", "很", "太", "还", "又", "再", "才", "已经", "一下",
    "说了", "说过", "说的", "时候", "现在", "今天", "昨天", "明天",
    # 这些组合几乎全是虚词，检索时只会把无关记忆捞上来
    "的事", "我的", "你的", "他的", "她的", "的事", "事情", "东西",
    "的人", "的话", "的话", "一起", "有点", "一点", "多少", "几个",
    "关于", "对于", "关于", "这个", "那个", "还有", "没有", "不是",
}

# 小于这个权重的词只算「弱旁证」，不能单独撑起一条结果
_MIN_STRONG_WEIGHT = 0.8

# ---------- 同义词 ----------
# 左 → 右：搜左边也能找到含右边的记忆
SYNONYMS: dict[str, tuple[str, ...]] = {
    "回消息": ("回复", "说话", "发消息", "聊天", "回话"),
    "啰嗦": ("话多", "太长", "别太满", "废话", "回三句", "缩句"),
    "骂": ("凶", "怼", "批评", "嫌", "说重话"),
    "开心": ("高兴", "快乐", "笑", "愉快"),
    "难过": ("伤心", "委屈", "哭", "难受", "低落"),
    "钱": ("花销", "花费", "账", "花了", "成本", "省钱"),
    "吃": ("饭", "吃东西", "外卖", "做饭"),
    "睡": ("睡觉", "熬夜", "睡醒", "起不来"),
    "游戏": ("原神", "鸣潮", "打游戏", "玩"),
    "工作": ("上班", "加班", "面试", "公司"),
    "学习": ("考试", "复习", "作业", "上课"),
    "唱歌": ("唱", "歌", "K歌"),
    "屏幕": ("截图", "看屏幕", "电脑上"),
    "记住": ("记得", "别忘了", "记着", "要记"),
    "正经": ("认真", "严肃", "嘱托", "交代"),
    "玩笑": ("逗", "开玩笑", "打趣", "闹"),
}

# 单字也要过滤（中文单字检索噪音太大）
_MIN_TERM_LEN = 2


def _clean(text: str) -> str:
    return re.sub(r"[\s\n\r\t]+", "", str(text or ""))


def extract_terms(query: str) -> list[tuple[str, float]]:
    """把查询拆成带权重的检索词。

    返回 [(词, 权重)]，权重越大越重要。已去重、已过滤停用词。
    """
    q = _clean(query)
    if not q:
        return []

    seen: dict[str, float] = {}

    def add(term: str, weight: float) -> None:
        term = str(term or "").strip()
        if len(term) < _MIN_TERM_LEN:
            return
        if term in STOPWORDS:
            return
        if term in seen and seen[term] >= weight:
            return
        seen[term] = weight

    # 1. 整句就算一个强词：短查询（「睡觉」「啰嗦」）整句本身就是主题
    if len(q) >= 2:
        add(q, 1.5 if len(q) >= 4 else 1.2)

    # 2. 滑窗取 2-4 字片段
    for size, weight in ((4, 1.0), (3, 0.8), (2, 0.5)):
        for i in range(0, max(0, len(q) - size + 1)):
            frag = q[i : i + size]
            if frag in STOPWORDS:
                continue
            # 片段里全是停用词就不算
            if all(c in "的了吗呢吧啊是在有和就都也我你他她" for c in frag):
                continue
            add(frag, weight)

    # 3. 同义词扩展。权重 0.85：它代表「用户真正想找的东西」，
    #    必须能过 _MIN_STRONG_WEIGHT 这道坎，否则同义词就跟没加一样。
    for key, others in SYNONYMS.items():
        if key in q:
            for alt in others:
                add(alt, 0.85)
        else:
            for alt in others:
                if alt in q:
                    add(key, 0.9)
                    add(alt, 0.9)

    items = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
    return items[:14]


def score_content(content: str, terms: list[tuple[str, float]]) -> tuple[float, int, float]:
    """给一条记忆打分。返回 (得分, 命中词数, 命中的最高词权重)。"""
    text = _clean(content)
    if not text or not terms:
        return 0.0, 0, 0.0

    total = 0.0
    hits = 0
    max_weight = 0.0
    for term, weight in terms:
        pos = text.find(term)
        if pos < 0:
            continue
        hits += 1
        max_weight = max(max_weight, weight)
        # 出现在开头/前部，更可能是这句话的主题
        if pos == 0:
            bonus = 1.35
        elif pos <= 4:
            bonus = 1.15
        elif pos <= 12:
            bonus = 1.0
        else:
            bonus = 0.9
        # 短文本里命中更重要（长文本命中很正常）
        length_factor = 1.1 if len(text) < 40 else 1.0
        total += weight * bonus * length_factor

    return total, hits, max_weight


def dedupe_by_similarity(
    items: Iterable[tuple[float, str, object]], threshold: float = 0.88
) -> list[tuple[float, str, object]]:
    """按内容相似度去重，保留分数最高的那条。

    items: [(score, content, payload)]
    """
    import difflib

    kept: list[tuple[float, str, object]] = []
    for score, content, payload in sorted(items, key=lambda x: x[0], reverse=True):
        norm = _clean(content).lower()
        dup = False
        for _, kept_content, _ in kept:
            other = _clean(kept_content).lower()
            if norm == other:
                dup = True
                break
            if not norm or not other:
                continue
            if norm in other or other in norm:
                dup = True
                break
            if difflib.SequenceMatcher(None, norm, other).ratio() >= threshold:
                dup = True
                break
        if not dup:
            kept.append((score, content, payload))
    return kept


def rerank(
    candidates: Iterable[object],
    query: str,
    *,
    content_of=lambda x: getattr(x, "content", ""),
    status_of=lambda x: getattr(x, "status", ""),
    importance_of=lambda x: getattr(x, "importance", 0.5),
    dormant_penalty: float = 0.3,
    candidate_penalty: float = 0.5,
    limit: int = 5,
    min_weight: float = _MIN_STRONG_WEIGHT,
) -> list[object]:
    """通用重排：给候选打分、去重、取前 N。

    min_weight：要求命中的词至少这么重（挡住「的事」这种弱词凑数）。
                传 0 表示不要求——语义召回的候选本来就不含查询原词，
                那时由调用方保证候选质量。
    """
    terms = extract_terms(query)
    if not terms:
        return []

    scored: list[tuple[float, str, object]] = []
    for obj in candidates:
        s, hits, max_w = score_content(content_of(obj), terms)
        if hits == 0 and min_weight > 0:
            continue
        # 只靠「的」「事」这种弱词命中的，不算找到——宁可返回空
        if min_weight > 0 and max_w < min_weight:
            continue
        status = str(status_of(obj) or "")
        if status == "dormant":
            s *= dormant_penalty
        elif status == "candidate":
            s *= candidate_penalty
        s *= max(0.1, float(importance_of(obj) or 0.5))
        scored.append((s, content_of(obj), obj))

    scored = dedupe_by_similarity(scored)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [obj for _, _, obj in scored[:limit]]


__all__ = [
    "STOPWORDS", "SYNONYMS", "extract_terms", "score_content",
    "dedupe_by_similarity", "rerank",
]

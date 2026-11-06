"""铃光记忆 · 定性（这句话是什么意思）。

六类 + 置信度三档：
  >= 0.7      正常定型
  0.6 - 0.7   自己标「多半是玩笑」先顶着，不吵主人
  < 0.6       拿不准，进待问队列

这是纯正则实现，不花钱。拿不准的交给问主人流程。
"""
from __future__ import annotations

import re

# (nature, 正则, 基础置信度)，顺序即优先级（越靠前越优先）
RULES: list[tuple[str, str, float]] = [
    ("scold", r"傻[逼b屄]|蠢货|废物|滚蛋|滚开|没用的东西|恶心|垃圾|白痴|智障|你特么", 0.82),
    ("request", r"以后|每次|记得|别忘了|不许|不准|要一直|从现在起|帮我|给我|必须|一定要", 0.78),
    ("joke", r"哈哈|233|笑死|逗你|开玩笑|狗头|😂|🤣|😆|你猜|真的假的|绝了|离谱|草(?![莓])", 0.75),
    ("emotion", r"好累|难过|想哭|难受|委屈|生气|高兴|害怕|开心|烦死|心累|崩溃", 0.72),
    ("question", r"[？?]|吗[。！!]*$|呢[。！!]*$|怎么|为什么|是不是|能不能|要不要|怎么办", 0.70),
]

NATURE_CN = {
    "joke": "玩笑",
    "scold": "真骂",
    "request": "正经嘱托",
    "fact": "陈述",
    "question": "提问",
    "emotion": "情绪",
    "joke_guess": "多半是玩笑",
}


def classify_nature(text: str) -> tuple[str, float]:
    """判断一句话的性质，返回 (nature, 置信度)。"""
    s = str(text or "").strip()
    if not s:
        return "fact", 0.55

    hits: list[tuple[str, float]] = []
    for nature, pattern, weight in RULES:
        if re.search(pattern, s, re.IGNORECASE):
            hits.append((nature, weight))

    if not hits:
        return "fact", 0.60

    order = {n: i for i, (n, _, _) in enumerate(RULES)}
    hits.sort(key=lambda kv: (order.get(kv[0], 99), -kv[1]))
    nature, conf = hits[0]

    # 命中越多越难判：3 条以上冲突直接落进「问主人」档
    if len(hits) >= 3:
        conf -= 0.20
    elif len(hits) == 2:
        conf -= 0.08

    return nature, max(0.3, min(0.95, conf))


def to_tier(nature: str, conf: float) -> tuple[str, str, bool]:
    """映射成三档：(最终 nature, 来源标记, 是否要问主人)。"""
    if conf >= 0.7:
        return nature, "rule", False
    if conf >= 0.6:
        return "joke_guess", "assume", False
    return "", "uncertain", True


def apply_history(
    nature: str, conf: float, history: list[dict]
) -> tuple[str, float, str]:
    """用历史修正定性。

    返回 (nature, conf, source)。
    - 历史一致且当前不够自信 → 提到 0.75
    - 当前拿不准但历史有明确判断 → 按历史顶到 0.62
    """
    if not history:
        return nature, conf, ""
    same = [h for h in history if str(h.get("nature") or "") == nature]
    if same and conf < 0.7:
        return nature, max(conf, 0.75), "history"
    if conf < 0.6:
        best = max(history, key=lambda h: float(h.get("nature_conf") or 0))
        hn = str(best.get("nature") or "")
        if hn and hn != "joke_guess":
            return hn, 0.62, "history_assume"
    return nature, conf, ""


__all__ = ["RULES", "NATURE_CN", "classify_nature", "to_tier", "apply_history"]

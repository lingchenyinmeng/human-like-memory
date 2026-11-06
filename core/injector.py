"""铃光记忆 · 注入器：把记忆整理成一段话塞进上下文。

三条规矩：
1. **带时间锚点**——每条都写「X 天前」，否则模型会把旧事当成现在
2. **按性质分类**——规矩、教导、约定、态度各归各的，不混成一坨
3. **红线**——群聊里一个字都不许带出「与主人的私聊」和「私密性」内容
"""
from __future__ import annotations

import time
from typing import Any

from .models import Atom, AtomStatus


def human_age(ts: float, now: float | None = None) -> str:
    """把时间戳变成「3 天前」这种人话。"""
    now = now or time.time()
    delta = max(0.0, now - float(ts or now))
    if delta < 3600:
        return f"{max(1, int(delta // 60))} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    days = int(delta // 86400)
    if days == 1:
        return "昨天"
    if days < 30:
        return f"{days} 天前"
    return f"{days // 30} 个月前"


def format_atom(atom: Atom, now: float | None = None) -> str:
    """一条记忆 → 一行带时间锚点的文本。"""
    now = now or time.time()
    when = time.strftime("%m-%d %H:%M", time.localtime(atom.create_time or now))
    age = human_age(atom.create_time, now)
    content = str(atom.content or "")
    # content 里已经写了「噩壤说…」就别再加一遍发言人
    who = f"{atom.who_name}：" if atom.who_name and not content.startswith(atom.who_name) else ""
    nature = {
        "joke": "玩笑", "joke_guess": "多半是玩笑", "scold": "真骂",
        "request": "嘱托", "question": "提问", "emotion": "情绪", "fact": "",
    }.get(atom.nature or "", "")
    tag = f"（{nature}）" if nature else ""
    return f"- {who}{content}{tag}  — {when}（{age}）"


class Injector:
    """把记忆整理成注入文本。"""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def build(
        self,
        atoms: list[Atom],
        *,
        is_group: bool = False,
        owner_id: str = "",
        max_chars: int = 400,
    ) -> str:
        """生成注入文本。空列表返回空串。"""
        now = time.time()
        cold = float(self.config.get("dormant_recall_penalty", 0.3) or 0.3)

        rules: list[Atom] = []
        teachings: list[Atom] = []
        promises: list[Atom] = []
        praise = blame = 0
        extras: list[Atom] = []

        for atom in atoms or []:
            # 红线：群聊里剔掉主人私聊和私密内容
            if is_group:
                if atom.with_owner:
                    continue
                if (atom.privacy or "") == "sensitive":
                    continue
            # 待定的只在同人检索时用，不主动进注入
            if atom.status == AtomStatus.DORMANT.value:
                continue
            if atom.mtype == "command":
                if atom.subtype == "rule":
                    rules.append(atom)
                elif atom.subtype == "teaching":
                    teachings.append(atom)
                # action（一次性任务）不进长期记忆
            elif atom.mtype == "promise":
                promises.append(atom)
            elif atom.mtype == "praise":
                praise += 1
            elif atom.mtype == "blame":
                blame += 1
            else:
                extras.append(atom)

        lines: list[str] = []
        if rules:
            lines.append("主人定下的规矩（要一直守）：")
            lines.extend(format_atom(a, now) for a in rules[:5])
        if teachings:
            lines.append("你被教过该怎么说：")
            lines.extend(format_atom(a, now) for a in teachings[:3])
        if promises:
            lines.append("你们之间有过这些约定：")
            lines.extend(format_atom(a, now) for a in promises[:3])
        if praise or blame:
            lines.append(f"他对你的态度：夸过 {praise} 次、批评过 {blame} 次")
        if extras:
            lines.append("最近的事：")
            lines.extend(format_atom(a, now) for a in extras[:5])

        if not lines:
            return ""

        head = (
            "[你记得的事]\n"
            f"（现在是 {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}，"
            "下面都发生在过去）\n"
        )
        tail = "\n[这些是背景，别机械复述，也别说「我记得」「你说过」；自然用。]"
        body = head + "\n".join(lines) + tail
        if len(body) > max_chars:
            body = body[: max_chars - 1] + "…]"
        return body


__all__ = ["Injector", "format_atom", "human_age"]

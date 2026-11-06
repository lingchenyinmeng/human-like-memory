"""Human-like Memory · 总结器：把一段对话变成一条记忆。

要点：
- 第一人称回顾，但必须写清「谁说了什么」——别人的话不许写进「我」嘴里
- 相对时间转成具体日期（尤其「明天」要 +1 天）
- 区分「明确要求」和「随口一句」
- 输出 JSON，解析失败就当没总结成功
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger

from .models import Memory

SYSTEM_PROMPT = """你在帮「铃光」整理记忆。下面是刚刚发生的一段对话，请提炼成一条可长期复用的记忆。

要求：
1. 用第一人称回顾，但必须写清**谁说了什么**。别人的话不能写成「我」说的。
   写「噩壤让我别啰嗦」「鲸宝说他今天很困」这种，不要写「我们聊了很多」。
2. 时间：把「今天」「明天」「昨天」这类说法换成具体日期。
   注意「明天」是消息发出那天的**下一天**，不要写成当天。
3. 区分「明确要求」和「随口一句」：只有带「以后」「每次」「记得」「必须」这类才算正经要求。
4. 你自己说过的话也要记，别只记别人对你说了什么。
5. 不写流水账，写清这件事的结论和关键事实。

只输出一个 JSON 对象，不要解释：
{
  "summary": "一段话概括这段对话（120 字以内）",
  "key_facts": ["事实1", "事实2"],
  "topics": ["话题"],
  "participants": ["参与者昵称"],
  "sentiment": "positive|neutral|negative",
  "importance": 0.5
}

key_facts 是给后续分类用的，尽量写得像「噩壤说以后回消息别太满」这种一句一事的形式。"""


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "未知时间"


def format_messages(messages: list[dict]) -> str:
    """把消息列表格式化成给模型看的对话文本。"""
    lines: list[str] = []
    for msg in messages:
        who = msg.get("sender_name") or msg.get("sender_id") or (
            "铃光" if msg.get("is_bot") else "某人"
        )
        ts = _fmt_time(msg.get("create_time") or time.time())
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{ts}] {who}: {content}")
    return "\n".join(lines)


class Summarizer:
    """调 LLM 生成记忆。"""

    def __init__(self, context, config: dict | None = None):
        self.context = context
        self.config = config or {}

    async def _call_llm(self, prompt: str) -> str:
        provider = None
        try:
            provider = self.context.get_using_provider()
        except Exception as e:
            logger.warning(f"[human_like_mem] 拿不到模型: {e!r}")
        if provider is None:
            return ""
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt=SYSTEM_PROMPT,
            )
            return str(getattr(resp, "completion_text", "") or "")
        except Exception as e:
            logger.warning(f"[human_like_mem] 总结调用失败: {e!r}")
            return ""

    async def summarize(
        self,
        messages: list[dict],
        *,
        session_id: str = "",
        persona_id: str = "",
        is_group: bool = False,
    ) -> Memory | None:
        """总结一段对话。失败返回 None。"""
        if not messages:
            return None
        text = format_messages(messages)
        if len(text) < 20:
            return None

        prompt = (
            f"下面是这段对话（{is_group and '群聊' or '私聊'}）：\n\n{text}\n\n"
            "现在按要求的 JSON 格式输出记忆。"
        )
        raw = await self._call_llm(prompt)
        if not raw:
            return None

        data = self._parse(raw)
        if not data:
            logger.warning(f"[human_like_mem] 总结结果解析失败: {raw[:200]}")
            return None

        participants = [str(x) for x in (data.get("participants") or []) if str(x).strip()]
        identities: list[dict] = []
        seen: set[str] = set()
        for msg in messages:
            sid = str(msg.get("sender_id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            identities.append(
                {
                    "sender_id": sid,
                    "display_name": str(msg.get("sender_name") or sid),
                    "aliases": [str(msg.get("sender_name") or "")],
                    "is_bot": bool(msg.get("is_bot")),
                }
            )

        summary = str(data.get("summary") or "").strip()
        key_facts = [str(x).strip() for x in (data.get("key_facts") or []) if str(x).strip()]
        try:
            importance = float(data.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5

        mem = Memory(
            content=summary or text[:500],
            summary=summary,
            personality_summary=summary,
            key_facts=key_facts,
            topics=[str(x) for x in (data.get("topics") or [])],
            participants=participants,
            participant_identities=identities,
            sentiment=str(data.get("sentiment") or "neutral"),
            importance=max(0.0, min(1.0, importance)),
            session_id=session_id,
            persona_id=persona_id,
            source_session_id=session_id,
            is_group=is_group,
            create_time=time.time(),
            metadata={"message_count": len(messages), "source": "summarizer"},
        )
        logger.info(
            f"[human_like_mem] 总结完成: {len(key_facts)} 条事实, 重要性 {importance:.2f}"
        )
        return mem

    @staticmethod
    def _parse(raw: str) -> dict | None:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
        return data if isinstance(data, dict) else None


__all__ = ["Summarizer", "format_messages", "SYSTEM_PROMPT"]

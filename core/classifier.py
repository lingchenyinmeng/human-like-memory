"""Human-like Memory · 分类（谁对谁 + 什么类型 + 子类型）与上下文提取。

输入是 key_facts（LLM 从对话里提炼的短句），输出是带标签的 Atom 雏形。
分类全部走规则，不调模型。
"""
from __future__ import annotations

import re
import time
from typing import Any

from .models import Atom, AtomStatus, MType
from .nature import apply_history, classify_nature, resolve_nature, to_tier

# ---------- 关键词表 ----------

_CMD = r"去|帮我|给我|马上|现在|立刻|不许|不准|别|以后|每次|记得|要一直|改成|教|学会|要求|让我|叫我|喊我|吩咐|叮嘱"
_PROMISE = r"答应|承诺|约定|说好|一定|我会|保证"
_PRAISE = r"可爱|厉害|聪明|好看|不错|挺好|棒|优秀|喜欢|真行|靠谱|夸"
_BLAME = r"蠢|笨|傻|烦|啰嗦|话多|点偏|错了|不行|没用|废物|滚|嫌|骂"
_EMOTION = r"累|困|难过|开心|烦|委屈|生气|害怕|想哭|难受|高兴|崩溃|爽"
_REQUEST = r"能不能|可不可以|帮我|请问|怎么办|要不要|吗[？?]?$|呢[？?]?$|为什么|是不是"
_SMALLTALK = r"^哈|^嗯|^哦|^在吗|^早|^晚|吃了|睡了|天气|晚安"

# command 细分
_RULE_WORDS = r"以后|每次|永远|不许|不准|别再|要一直|从现在起|记住|规矩|定个规矩"
_TEACH_WORDS = r"教你|应该|改成|别用|学会|这样说|这样说才对|下次这么|教你一句"
_ACTION_WORDS = r"去|帮我|给我|现在|赶紧|马上"

# blame 细分
_IDENTITY_WORDS = r"傻|蠢|笨|废物|没用|垃圾|滚|你是鱼|智障"

# 隐私关键词（情感/隐私类 → sensitive）
_SENSITIVE = r"难过|委屈|哭|分手|吵架|家里|身体|病|钱|工资|工资|秘密|不想说|心事|失眠|焦虑|压力"


def _has(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, str(text or "")))


def classify_type(text: str) -> tuple[str, float]:
    """判 type（什么性质的话）。"""
    t = str(text or "")
    if _has(_PROMISE, t):
        return MType.PROMISE.value, 0.78
    if _has(_PRAISE, t):
        return MType.PRAISE.value, 0.75
    if _has(_BLAME, t):
        return MType.BLAME.value, 0.76
    if _has(_CMD, t):
        return MType.COMMAND.value, 0.74
    if _has(_EMOTION, t):
        return MType.EMOTION.value, 0.72
    if _has(_REQUEST, t):
        return MType.REQUEST.value, 0.70
    if _has(_SMALLTALK, t):
        return MType.SMALLTALK.value, 0.65
    return MType.FACT.value, 0.60


def classify_subtype(mtype: str, text: str) -> str:
    """只给 command / fact / blame 三种细分。"""
    t = str(text or "")
    if mtype == MType.COMMAND.value:
        if _has(_RULE_WORDS, t):
            return "rule"
        if _has(_TEACH_WORDS, t):
            return "teaching"
        return "action"
    if mtype == MType.BLAME.value:
        return "identity" if _has(_IDENTITY_WORDS, t) else "behavior"
    if mtype == MType.FACT.value:
        # 有人称 + 描述 → person；纯事件 → event
        if _has(r"是|叫|喜欢|住在|年龄|生日|职业|工作", t) and _has(r"[一-龥]{2,4}", t):
            return "person"
        return "event"
    return ""


def classify_layer(
    text: str,
    *,
    owner_id: str = "",
    who_id: str = "",
    who_name: str = "",
    source: str = "",
    identities: list[dict] | None = None,
) -> str:
    """判 layer（谁对谁）。"""
    t = str(text or "")
    owner = str(owner_id or "").strip()
    who = str(who_id or "").strip()
    is_owner = bool(owner and who == owner)
    name_to_id: dict[str, str] = {}
    for ident in identities or []:
        if not isinstance(ident, dict):
            continue
        sid = str(ident.get("sender_id") or "").strip()
        if not sid:
            continue
        for key in ("display_name", *(ident.get("aliases") or [])):
            k = str(key or "").strip()
            if k:
                name_to_id[k] = sid

    # 句子里提到谁？看名字前缀
    mentioned = ""
    for name in name_to_id:
        if name and t.startswith(name):
            mentioned = name
            break

    bot_marked = bool(re.search(r"铃光|我|自己", t[:4]))
    if mentioned:
        target_is_owner = bool(owner and name_to_id.get(mentioned) == owner)
        speaker_is_owner = is_owner
        speaker_is_bot = bot_marked
        if speaker_is_bot:
            return "me_to_owner" if target_is_owner else "me_to_other"
        if speaker_is_owner:
            return "owner_to_me" if target_is_owner is False and mentioned in ("铃光", "我") else "owner_to_other"
        return "other_to_me" if mentioned in ("铃光", "我") else "other_to_other"

    # 没提名字：按当前说话人推
    if bot_marked:
        return "me_to_owner" if is_owner else "me_to_other"
    if is_owner:
        return "owner_to_me"
    if source == "group":
        return "other_to_other"
    return "other_to_me"


def is_sensitive(text: str, mtype: str, nature: str) -> bool:
    """情感/隐私类内容 → 打 sensitive 标签（红线第二档靠它）。"""
    if mtype == MType.EMOTION.value or nature == "emotion":
        return True
    return _has(_SENSITIVE, str(text or ""))


def extract_context(messages: list, count: int = 2, max_chars: int = 100) -> list[dict]:
    """取最近 N 条消息当上文。"""
    out: list[dict] = []
    for msg in list(messages)[-max(1, count):]:
        try:
            text = str(getattr(msg, "content", "") or "").strip()
        except Exception:
            text = ""
        if not text:
            continue
        try:
            name = str(getattr(msg, "sender_name", "") or getattr(msg, "sender_id", "") or "")
        except Exception:
            name = ""
        out.append({"who": name, "text": text[:max_chars]})
    return out


def build_atoms(
    key_facts: list[str],
    *,
    memory_id: int = 0,
    owner_id: str = "",
    who_id: str = "",
    who_name: str = "",
    source: str = "",
    said_at: float = 0.0,
    context: list[dict] | None = None,
    identities: list[dict] | None = None,
    history: list[dict] | None = None,
    importance: float = 0.5,
    session_id: str = "",
    persona_id: str = "",
    topic: str = "",
) -> list[Atom]:
    """把 key_facts 变成带完整标签的原子列表。"""
    atoms: list[Atom] = []
    now = time.time()
    for fact in key_facts:
        fact = str(fact or "").strip()
        if not fact:
            continue

        mtype, tconf = classify_type(fact)
        subtype = classify_subtype(mtype, fact)
        layer = classify_layer(
            fact, owner_id=owner_id, who_id=who_id,
            who_name=who_name, source=source, identities=identities,
        )

        # 定性：按 mtype 定基调，正则只细化语气（避免两套规则打架）
        raw_nature, raw_conf = resolve_nature(mtype, fact)
        nature, nconf, nsrc = apply_history(raw_nature, raw_conf, history or [])
        final_nature, tier_src, need_ask = to_tier(nature, nconf)
        if nsrc:
            tier_src = nsrc

        privacy = "sensitive" if is_sensitive(fact, mtype, final_nature) else "normal"
        with_owner = bool(
            owner_id and who_id == owner_id and source == "private"
        )

        atom = Atom(
            memory_id=memory_id,
            content=fact,
            layer=layer,
            mtype=mtype,
            subtype=subtype,
            classify_conf=tconf,
            nature=final_nature,
            nature_conf=nconf,
            nature_source=tier_src,
            who_id=who_id,
            who_name=who_name,
            said_at=float(said_at or now),
            context=list(context or []),
            source=source,
            with_owner=with_owner,
            topic=topic,
            privacy=privacy,
            status=AtomStatus.CANDIDATE.value,
            importance=float(importance),
            create_time=now,
            last_access=now,
            session_id=session_id,
            persona_id=persona_id,
            metadata={},
        )
        if need_ask:
            atom.metadata["ask_owner"] = True
            atom.metadata["nature_guess"] = raw_nature
            atom.metadata["nature_conf"] = round(raw_conf, 2)
        atoms.append(atom)
    return atoms


__all__ = [
    "classify_type", "classify_subtype", "classify_layer",
    "is_sensitive", "extract_context", "build_atoms",
]

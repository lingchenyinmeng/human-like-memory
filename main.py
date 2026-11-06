"""Human-like Memory · 独立记忆插件。

从 livingmemory 的改造里抽出来的一套精简实现：
- 双层记忆：短时库（candidate）+ 正式库（active/dormant）
- 三级分类：谁对谁 × 什么类型 × 子类型
- 定性：玩笑/真骂/嘱托/事实/提问/情绪 + 置信度三档
- 分区：群聊按时间间隔、私聊按话题
- 回溯比对：同一个人以前说过类似的话吗
- 红线：群聊里绝不带出主人私聊和私密内容
- 纠正入口：主人说判错了就能改

不依赖 livingmemory，也不需要向量库（用 FTS）。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.classifier import build_atoms, extract_context
from .core.conversation import ConversationStore
from .core.injector import Injector
from .core.models import Atom, AtomStatus, Memory
from .core.store import MemoryStore
from .core.summarizer import Summarizer


class LingguangMemory(Star):
    """独立记忆插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_human_like_memory"))
        os.makedirs(self.data_dir, exist_ok=True)
        self.memory_db = os.path.join(self.data_dir, "memory.db")
        self.conv_db = os.path.join(self.data_dir, "conversations.db")

        self.store: MemoryStore | None = None
        self.conv: ConversationStore | None = None
        self.summarizer: Summarizer | None = None
        self.injector: Injector | None = None
        self._loop_task: asyncio.Task | None = None
        self._last_ask = 0.0
        self._backfill_seen: set[str] = set()

    # ---------- 配置 ----------

    def _cfg(self) -> dict:
        return {
            "staging_enabled": bool(self.conf.get("staging_enabled", True)),
            "staging_ttl_hours": float(self.conf.get("staging_ttl_hours", 48) or 48),
            "staging_similarity_threshold": float(
                self.conf.get("staging_similarity_threshold", 0.85) or 0.85
            ),
            "importance_drop_threshold": float(
                self.conf.get("importance_drop_threshold", 0.3) or 0.3
            ),
            "dormant_keep_days": float(self.conf.get("dormant_keep_days", 7) or 7),
            "promote_min_reinforce": int(self.conf.get("promote_min_reinforce", 2) or 2),
            "promote_min_confidence": float(
                self.conf.get("promote_min_confidence", 0.8) or 0.8
            ),
            "staging_recall_penalty": float(
                self.conf.get("staging_recall_penalty", 0.5) or 0.5
            ),
            "group_partition_gap_minutes": int(
                self.conf.get("group_partition_gap_minutes", 15) or 15
            ),
            "backfill_enabled": bool(self.conf.get("backfill_enabled", True)),
            "backfill_lookback_days": float(
                self.conf.get("backfill_lookback_days", 30) or 30
            ),
        }

    @property
    def owner_id(self) -> str:
        qq = str(self.conf.get("owner_qq") or "").strip()
        if qq:
            return qq
        try:
            cfg = self.context.get_config() or {}
            for a in cfg.get("admins_id") or []:
                a = str(a).strip()
                if a and a != "astrbot":
                    return a
        except Exception:
            pass
        return ""

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        owner = self.owner_id
        if not owner:
            return True
        try:
            return str(event.get_sender_id() or "") == owner
        except Exception:
            return False

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        self.store = MemoryStore(self.memory_db, self._cfg())
        await self.store.initialize()
        self.conv = ConversationStore(self.conv_db)
        await self.conv.initialize()
        self.summarizer = Summarizer(self.context, self._cfg())
        self.injector = Injector(self._cfg())

        # 语义检索（可选）：装了 Ollama + bge-m3 且建过索引才启用
        await self._init_semantic()

        self._loop_task = asyncio.create_task(self._loop(), name="lingguang-mem-loop")
        logger.info(
            f"[human_like_mem] 已启动 | 库: {self.memory_db} | "
            f"暂存 {self._cfg()['staging_ttl_hours']:.0f}h"
        )

    async def _init_semantic(self) -> None:
        """按配置决定要不要开语义检索。失败一律降级为纯关键词，不抛。"""
        mode = str(self.conf.get("semantic_enabled", "auto") or "auto").strip().lower()
        if mode == "off":
            logger.info("[human_like_mem] 语义检索：按配置关闭")
            return
        if self.store is None:
            return
        try:
            from .core.semantic import EmbeddingClient, VectorIndex

            client = EmbeddingClient(
                base_url=str(self.conf.get("semantic_base_url", "http://127.0.0.1:11434")),
                model=str(self.conf.get("semantic_model", "bge-m3")),
            )
            if not await client.available():
                if mode == "on":
                    logger.warning(
                        "[human_like_mem] 语义检索开着但 Ollama 不可用，本次退回纯关键词"
                    )
                else:
                    logger.info("[human_like_mem] 没检测到 Ollama，语义检索自动跳过")
                return

            index = VectorIndex(self.memory_db)
            await index.initialize()
            if not await index.load():
                logger.info(
                    "[human_like_mem] 还没有向量索引；跑 tools/build_embeddings.py 建一次就能用上"
                )
                self.store.embedding_client = client
                self.store.vector_index = index
                return

            self.store.embedding_client = client
            self.store.vector_index = index
            logger.info(
                f"[human_like_mem] 语义检索已启用（{self.conf.get('semantic_model', 'bge-m3')}）"
            )
        except Exception as e:
            logger.warning(f"[human_like_mem] 语义检索初始化失败，退回纯关键词: {e!r}")

    async def terminate(self) -> None:
        task = self._loop_task
        self._loop_task = None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                pass
        logger.info("[human_like_mem] 已停止")

    async def _loop(self) -> None:
        """后台：定期总结 + 生命周期 + 问主人。"""
        await asyncio.sleep(int(self.conf.get("startup_delay_seconds", 90) or 90))
        while True:
            try:
                await self._maintain()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[human_like_mem] 维护出错: {e!r}")
            await asyncio.sleep(int(self.conf.get("maintain_interval_minutes", 10) or 10) * 60)

    async def _maintain(self) -> None:
        if self.store is None or self.conv is None:
            return
        # 1. 生命周期：暂存 → 待定 / 正式 → 遗忘库 / 遗忘库 → 真删
        try:
            await self.store.expire_stale()
            await self.store.purge_dormant()
            await self.store.purge_archived()
        except Exception as e:
            logger.warning(f"[human_like_mem] 生命周期处理失败: {e!r}")
        # 2. 给新记忆补向量（语义检索开着才做）
        try:
            await self._index_missing_vectors()
        except Exception as e:
            logger.warning(f"[human_like_mem] 补向量失败: {e!r}")
        # 3. 问主人
        try:
            await self._ask_owner()
        except Exception as e:
            logger.warning(f"[human_like_mem] 问主人失败: {e!r}")

    async def _index_missing_vectors(self, batch: int = 64) -> int:
        """给还没向量的原子补上（新记忆不会漏掉语义检索）。"""
        store = self.store
        if store is None or store.vector_index is None or store.embedding_client is None:
            return 0
        if not await store.embedding_client.available():
            return 0
        try:
            async with store._connect() as db:
                cursor = await db.execute(
                    "SELECT id, content FROM atoms WHERE status IN ('active','candidate') "
                    "ORDER BY id DESC LIMIT 500"
                )
                rows = await cursor.fetchall()
        except Exception as e:
            logger.debug(f"[human_like_mem] 取待建向量原子失败: {e!r}")
            return 0
        if not rows:
            return 0
        id_to_text = {int(r[0]): str(r[1] or "") for r in rows}
        missing = await store.vector_index.missing_ids(list(id_to_text.keys()), limit=batch)
        if not missing:
            return 0
        texts = [id_to_text[a] for a in missing]
        vecs = await store.embedding_client.embed_batch(texts)
        pairs = [(a, v) for a, v in zip(missing, vecs) if v is not None]
        if pairs:
            await store.vector_index.save_many(
                pairs, model=str(self.conf.get("semantic_model", "bge-m3"))
            )
            await store.vector_index.load(force=True)
            logger.info(f"[human_like_mem] 新记忆补向量 {len(pairs)} 条")
        return len(pairs)

    # ---------- 存消息 ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=95)
    async def _capture(self, event: AstrMessageEvent):
        """把每条消息先存下来（不拦、不改）。"""
        if self.conv is None:
            return
        try:
            session_id = str(event.unified_msg_origin or "")
            content = str(getattr(event, "message_str", "") or "").strip()
            if not session_id or not content:
                return
            await self.conv.add_message(
                session_id,
                content,
                sender_id=str(event.get_sender_id() or ""),
                sender_name=str(event.get_sender_name() or ""),
                role="user",
                group_id=str(event.get_group_id() or ""),
                platform=str(event.get_platform_name() or ""),
                is_bot=False,
                create_time=time.time(),
            )
            # 够了就总结
            threshold = int(self.conf.get("summary_trigger_messages", 30) or 30)
            n = await self.conv.count_unsummarized(session_id)
            if n >= threshold:
                asyncio.create_task(self._summarize_session(session_id))
        except Exception as e:
            logger.debug(f"[human_like_mem] 存消息失败: {e!r}")

    # ---------- 总结 ----------

    async def _summarize_session(self, session_id: str) -> None:
        if not (self.conv and self.store and self.summarizer):
            return
        try:
            msgs = await self.conv.unsummarized(session_id, limit=200)
            if len(msgs) < 6:
                return
            is_group = "GroupMessage" in session_id
            persona = ""
            try:
                persona = str(self.context.get_config().get("default_personality") or "")
            except Exception:
                pass
            mem = await self.summarizer.summarize(
                msgs, session_id=session_id, persona_id=persona, is_group=is_group
            )
            if mem is None:
                return
            await self.store.add_memory(mem)
            atom_objs = await self._build_atoms_for(mem, msgs)
            if atom_objs:
                await self.store.add_atoms(atom_objs)
            await self.conv.mark_summarized(session_id, int(msgs[-1]["id"]))
            logger.info(
                f"[human_like_mem] {session_id} 总结完成: "
                f"记忆 #{mem.memory_id}, {len(atom_objs)} 个原子"
            )
        except Exception as e:
            logger.error(f"[human_like_mem] 总结会话失败: {e!r}", exc_info=True)

    async def _build_atoms_for(self, mem: Memory, msgs: list[dict]) -> list[Atom]:
        if self.store is None:
            return []
        # 主要发言人：非 bot 的第一个
        who_id, who_name = "", ""
        for m in msgs:
            if not m.get("is_bot") and m.get("sender_id"):
                who_id = str(m["sender_id"])
                who_name = str(m.get("sender_name") or "")
                break
        source = "group" if mem.is_group else "private"
        said_at = float(msgs[-1].get("create_time") or time.time()) if msgs else time.time()

        # 上下文
        ctx = extract_context(
            [type("M", (), {"content": m.get("content"), "sender_name": m.get("sender_name")})()
             for m in msgs[-3:]],
            count=int(self.conf.get("context_messages", 2) or 2),
            max_chars=int(self.conf.get("context_max_chars", 100) or 100),
        )

        # 回溯比对
        history: list[dict] = []
        cfg = self._cfg()
        if cfg["backfill_enabled"] and who_id and mem.key_facts:
            try:
                history = await self.store.find_related_history(
                    who_id, str(mem.key_facts[0]), days=cfg["backfill_lookback_days"]
                )
            except Exception as e:
                logger.debug(f"[human_like_mem] 回溯失败: {e!r}")

        return build_atoms(
            mem.key_facts,
            memory_id=mem.memory_id,
            owner_id=self.owner_id,
            who_id=who_id,
            who_name=who_name,
            source=source,
            said_at=said_at,
            context=ctx,
            identities=mem.participant_identities,
            history=history,
            importance=mem.importance,
            session_id=mem.session_id,
            persona_id=mem.persona_id,
            topic=",".join(mem.topics[:1]),
        )

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def _inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if self.store is None or self.injector is None:
            return
        if not bool(self.conf.get("inject_enabled", True)):
            return
        try:
            is_group = "GroupMessage" in str(event.unified_msg_origin or "")
            hot_n = int(self.conf.get("hot_memory_count", 3) or 3)
            query = str(getattr(event, "message_str", "") or "").strip()
            # 热记忆按相关性挑（有查询时），没查询才退回「最近动过的」
            hot = await self.store.hot_atoms(query=query, hours=48, limit=hot_n)
            cold = (
                await self.store.search_atoms(
                    query, limit=int(self.conf.get("recall_top_k", 5) or 5)
                )
                if query
                else []
            )
            merged: list[Atom] = []
            seen: set[int] = set()
            for a in (hot + cold):
                if a.atom_id in seen:
                    continue
                seen.add(a.atom_id)
                merged.append(a)
            text = self.injector.build(
                merged,
                is_group=is_group,
                owner_id=self.owner_id,
                max_chars=int(self.conf.get("inject_max_chars", 400) or 400),
            )
            if text:
                from astrbot.api.message_components import Plain  # noqa: F401
                from astrbot.core.message.components import TextPart

                req.extra_user_content_parts.append(
                    TextPart(text=text).mark_as_temp()
                )
                logger.debug(f"[human_like_mem] 注入 {len(merged)} 条记忆")
        except Exception as e:
            logger.warning(f"[human_like_mem] 注入失败: {e!r}")

    # ---------- 问主人 ----------

    async def _ask_owner(self) -> None:
        if self.store is None:
            return
        cooldown = float(self.conf.get("ask_cooldown_minutes", 10) or 10) * 60
        if time.time() - self._last_ask < cooldown:
            return
        atoms = await self.store.find_pending_asks(
            int(self.conf.get("ask_max_per_time", 5) or 5)
        )
        if not atoms:
            return
        owner = self.owner_id
        if not owner:
            return
        platform = await self._platform_name()
        if not platform:
            return

        lines = ["有几句话我拿不准是什么意思，你帮我判一下："]
        for i, a in enumerate(atoms, 1):
            guess = ""
            if isinstance(a.metadata, dict):
                guess = str(a.metadata.get("nature_guess") or "")
            tail = f"（我猜是{guess}，不敢定）" if guess else "（我判不出来）"
            who = f"{a.who_name}：" if a.who_name else ""
            lines.append(f"{i}. {who}{a.content[:60]} {tail}")
        lines.append("")
        lines.append("回我一声就行，我记下来以后就按这个判。")

        try:
            await self.context.send_message(
                f"{platform}:FriendMessage:{owner}",
                MessageChain().message("\n".join(lines)),
            )
            await self.store.mark_asked([a.atom_id for a in atoms])
            self._last_ask = time.time()
            logger.info(f"[human_like_mem] 已问主人 {len(atoms)} 条")
        except Exception as e:
            logger.warning(f"[human_like_mem] 发问失败: {e!r}")

    async def _platform_name(self) -> str:
        try:
            for p in getattr(self.context.platform_manager, "platform_insts", []) or []:
                pid = str(getattr(p, "id", "") or getattr(p, "name", "") or "").strip()
                if pid:
                    return pid
        except Exception:
            pass
        # 兜底：从记忆里的真实会话 ID 推
        try:
            if self.store:
                async with self.store._connect() as db:
                    cur = await db.execute(
                        "SELECT source_session_id FROM memories "
                        "WHERE source_session_id LIKE '%:%:%' ORDER BY id DESC LIMIT 1"
                    )
                    row = await cur.fetchone()
                if row and row[0]:
                    return str(row[0]).split(":")[0].strip()
        except Exception:
            pass
        return ""

    # ---------- 工具 ----------

    @filter.llm_tool(name="recall_memory")
    async def recall_memory(self, event: AstrMessageEvent, query: str = "", limit: int = 5):
        '''查自己的长期记忆。主人问「你记得…」「我们之前说过…」时用。

        Args:
            query(string): 要查什么，比如「主人对回消息的要求」
            limit(number): 最多返回几条，默认 5
        '''
        if self.store is None:
            return "记忆还没准备好。"
        q = str(query or getattr(event, "message_str", "") or "").strip()
        atoms = await self.store.search_atoms(q, limit=max(1, min(20, int(limit or 5))))
        if not atoms:
            return "没想起来相关的。"
        lines = ["想起来这些："]
        for a in atoms:
            tag = f"[{a.nature}]" if a.nature else ""
            lines.append(f"- ({a.atom_id}) {a.content} {tag}")
            await self.store.touch(a.atom_id)
        return "\n".join(lines)

    @filter.llm_tool(name="remember_this")
    async def remember_this(self, event: AstrMessageEvent, content: str = "", importance: float = 0.8):
        '''主人明确让你记住某件事时用（「记住这个」「别忘了」）。

        Args:
            content(string): 要记住的事，一句话说清
            importance(number): 重要性 0~1，默认 0.8
        '''
        if self.store is None or self.conv is None:
            return "记忆还没准备好。"
        text = str(content or "").strip()
        if not text:
            return "要记的内容是空的。"
        try:
            session_id = str(event.unified_msg_origin or "")
            mem = Memory(
                content=text, summary=text, personality_summary=text,
                key_facts=[text], importance=max(0.0, min(1.0, float(importance))),
                session_id=session_id, source_session_id=session_id,
                is_group="GroupMessage" in session_id,
                metadata={"source": "tool_memorize", "pinned": True},
            )
            await self.store.add_memory(mem)
            atom = Atom(
                memory_id=mem.memory_id, content=text,
                mtype="fact", nature="fact", nature_conf=0.9, nature_source="owner",
                who_id=str(event.get_sender_id() or ""),
                who_name=str(event.get_sender_name() or ""),
                said_at=time.time(), source="group" if "GroupMessage" in session_id else "private",
                status=AtomStatus.ACTIVE.value, importance=mem.importance,
                session_id=session_id,
                metadata={"pinned": True},
            )
            await self.store.add_atoms([atom], enable_dedup=False)
            return f"记住了：{text[:50]}"
        except Exception as e:
            logger.error(f"[human_like_mem] 记忆写入失败: {e!r}")
            return "没记上。"

    @filter.llm_tool(name="correct_memory")
    async def correct_memory(
        self, event: AstrMessageEvent, atom_id: int = 0, keyword: str = "",
        new_nature: str = "", reason: str = "",
    ):
        '''主人说某条记忆判错了（比如「这条不是玩笑」）时用，把它的性质改过来。

        Args:
            atom_id(number): 记忆 id，知道的话直接给
            keyword(string): 不知道 id 就用关键词找
            new_nature(string): 改成什么：joke/scold/request/fact/question/emotion
            reason(string): 为什么判错（可选）
        '''
        valid = ("joke", "scold", "request", "fact", "question", "emotion")
        n = str(new_nature or "").strip()
        if n not in valid:
            return f"性质只能是：{'/'.join(valid)}"
        if self.store is None:
            return "记忆还没准备好。"
        target = None
        if atom_id:
            target = await self.store.get_atom(int(atom_id))
        elif keyword:
            hits = await self.store.search_atoms(keyword, limit=3)
            target = hits[0] if hits else None
        if target is None:
            return "没找到要改的那条。"
        ok = await self.store.update_nature(target.atom_id, n)
        if not ok:
            return "改的时候出错了。"
        # 记下来，以后定性参考
        try:
            path = os.path.join(self.data_dir, "corrections.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(), "atom_id": target.atom_id,
                    "content": target.content[:200],
                    "old": target.nature, "new": n, "reason": str(reason or "")[:200],
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return f"改好了：从「{target.nature or '未定性'}」改成「{n}」"

    @filter.llm_tool(name="memory_status")
    async def memory_status(self, event: AstrMessageEvent):
        '''看一眼自己的记忆状况（有多少条、各状态多少）。'''
        if self.store is None:
            return "记忆还没准备好。"
        stats = await self.store.get_stats()
        atoms = stats.get("atoms", {})
        parts = [f"{k} {v}" for k, v in atoms.items()]
        return (
            f"记忆 {stats.get('memories', 0)} 条，原子 {sum(atoms.values())} 个："
            + "、".join(parts)
        )


__all__ = ["LingguangMemory"]

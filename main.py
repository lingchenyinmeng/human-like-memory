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

        # 人际关系库（跟事件库物理分开）
        try:
            from .core.people import PeopleStore

            self.people_db_path = os.path.join(self.data_dir, "people.db")
            self.people = PeopleStore(self.people_db_path)
            await self.people.initialize()
            logger.info(f"[human_like_mem] 人库已接上: {self.people_db_path}")
        except Exception as e:
            self.people = None
            logger.warning(f"[human_like_mem] 人库初始化失败（不影响记忆功能）: {e!r}")

        # 面板 API（页面在 views/dashboard/）
        try:
            from .core.page_api import PageApi

            self.page_api = PageApi(self)
            self.page_api.register_routes()
            logger.info("[human_like_mem] 面板 API 已注册")
        except Exception as e:
            logger.warning(f"[human_like_mem] 面板 API 注册失败（不影响记忆功能）: {e!r}")

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
        # 1.5 人原子也会冷下去（但不删，没关系的人依然在库里）
        try:
            if self.people is not None:
                await self.people.decay_people(
                    quiet_days=float(self.conf.get("people_quiet_days", 7) or 7),
                    archive_days=float(self.conf.get("people_archive_days", 90) or 90),
                )
        except Exception as e:
            logger.debug(f"[human_like_mem] 人原子衰减失败: {e!r}")
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
                # 挂 person_id：一条事件可能同时牵扯好几个人
                await self._attach_person_ids(atom_objs, mem.participant_identities)
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

    async def _attach_person_ids(self, atoms: list[Atom], identities: list[dict]) -> None:
        """给每条原子挂上人库里的 person_id（不写名字，只写 id）。"""
        if not atoms or self.people is None:
            return
        # 先把这条记忆涉及的人都认一遍：QQ 优先，昵称其次
        cache: dict[tuple[str, str], str] = {}

        async def pid_for(qq: str, name: str) -> str:
            key = (qq, name)
            if key in cache:
                return cache[key]
            try:
                pid = await self.people.resolve(
                    qq=qq, name=name, create_if_missing=True, source="inferred"
                )
            except Exception as e:
                logger.debug(f"[human_like_mem] 认人失败: {e!r}")
                pid = ""
            cache[key] = pid
            return pid

        # 记忆级参与者（一条事件可能牵扯好几个人）
        ident_map: dict[str, str] = {}
        for ident in identities or []:
            if not isinstance(ident, dict):
                continue
            sid = str(ident.get("sender_id") or "").strip()
            dname = str(ident.get("display_name") or "").strip()
            if not sid and not dname:
                continue
            pid = await pid_for(sid, dname)
            if pid:
                for nm in (dname, *(ident.get("aliases") or [])):
                    nm = str(nm or "").strip()
                    if nm:
                        ident_map[nm] = pid
                ident_map[sid] = pid

        for atom in atoms:
            ids: list[str] = []
            if atom.who_id or atom.who_name:
                pid = await pid_for(atom.who_id, atom.who_name)
                if pid:
                    ids.append(pid)
            # 正文里提到的人也算「在场」
            for name, pid in ident_map.items():
                if not name or len(name) < 2:
                    continue
                if name in atom.content and pid not in ids:
                    ids.append(pid)
            atom.person_ids = ids

    # ---------- 注入 ----------

    # ---------- 检索路由：什么时候该补关系 ----------

    _REL_HARD_WORDS = ("谁", "关系", "还算不算", "他现在是什么", "是什么关系", "什么人")

    async def _maybe_relations(self, query: str, atoms: list[Atom]) -> str:
        """按定的六条规则决定要不要查关系库，要查就顺手补上。

        规则：
          ① 问题里有「谁/关系/还算不算/他现在是什么」→ 硬规则，直接查
          ② 同一个 person_id 出现 ≥2 次，且这些人最近 7 天有变动 → 查
          ③ 事件结果全在同一话题、没有任何人际线索 → 查（防着"看起来够了"）
          反向：事件结果里已经写明关系 → 跳过
        """
        if self.people is None or not atoms:
            return ""
        import re as _re

        q = str(query or "")
        reasons: list[str] = []

        # 反向：正文里已经出现「是…的…」这种关系描述，就不用再查
        if _re.search(r"是.{1,6}的(主人|朋友|家人|恋人|同事|同学|师父|徒弟)", " ".join(a.content for a in atoms)):
            return ""

        # ① 硬规则
        if any(w in q for w in self._REL_HARD_WORDS):
            reasons.append("问题里问了关系")

        # 收集涉及的人
        person_ids: list[str] = []
        for a in atoms:
            for pid in (a.person_ids or []):
                if pid and pid not in person_ids:
                    person_ids.append(pid)
        if not person_ids:
            return ""

        # ② 同一个人出现多次 + 最近有变动
        if not reasons:
            from collections import Counter
            cnt = Counter(pid for a in atoms for pid in (a.person_ids or []))
            hot = [pid for pid, n in cnt.items() if n >= 2]
            if hot:
                week_ago = time.time() - 7 * 86400
                try:
                    for pid in hot[:3]:
                        rels = await self.people.relations_of(pid)
                        if any(float(r.get("updated_at") or 0) >= week_ago for r in rels):
                            reasons.append("同一个人反复出现，且最近关系有变动")
                            break
                except Exception:
                    pass

        # ③ 全在同一话题、没人际线索
        if not reasons:
            topics = {a.topic for a in atoms if a.topic}
            if len(topics) <= 1 and not any(a.layer in ("owner_to_other", "me_to_other", "other_to_other") for a in atoms):
                reasons.append("结果全在同一话题，补一下人际线索")

        if not reasons:
            return ""

        # 查这些人现在的关系
        lines: list[str] = []
        for pid in person_ids[:4]:
            try:
                card = await self.people.describe(pid)
            except Exception:
                continue
            if not card.get("relations"):
                continue
            name = ""
            for t in card.get("tags") or []:
                if t.get("kind") in ("nickname", "real_name") and t.get("value"):
                    name = str(t["value"])
                    break
            name = name or pid
            for r in card["relations"][:3]:
                other = r["b_id"] if r["a_id"] == pid else r["a_id"]
                guess = "（我猜的）" if r.get("source") == "inferred" else ""
                lines.append(f"- {name} 跟 {other} 是{r.get('rel_type')}{guess}")
        if not lines:
            return ""
        logger.debug(f"[human_like_mem] 补关系（{reasons[0]}）")
        return "[你记得的人际关系]\n" + "\n".join(lines[:6])
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
            # 按六条规则决定要不要补人际关系
            try:
                rel_text = await self._maybe_relations(query, merged)
            except Exception as e:
                logger.debug(f"[human_like_mem] 补关系失败: {e!r}")
                rel_text = ""
            if rel_text:
                text = (text + "\n" + rel_text) if text else rel_text
            if text:
                # TextPart 在 astrbot.core.agent.message，不在 core.message.components
                from astrbot.core.agent.message import TextPart

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

    # ---------- 人际关系的工具 ----------

    @filter.llm_tool(name="who_is")
    async def who_is(self, event: AstrMessageEvent, name: str = "", qq: str = ""):
        '''查一个人：他有哪些叫法、跟谁是什么关系。主人问「某某是谁」「他跟我什么关系」时用。

        Args:
            name(string): 名字/昵称/外号
            qq(string): QQ 号（知道的话更准）
        '''
        if self.people is None:
            return "人库还没准备好。"
        try:
            pid = await self.people.resolve(qq=qq, name=name, create_if_missing=False)
            if not pid or str(pid).startswith("p_unk_"):
                return f"我没认出来 {name or qq} 是谁。"
            card = await self.people.describe(pid)
            lines = [f"{name or qq}（{pid}）"]
            if card["tags"]:
                calls = "、".join(f"{t['value']}({t['kind']})" for t in card["tags"][:6])
                lines.append(f"叫法：{calls}")
            if card["relations"]:
                lines.append("关系：")
                for r in card["relations"][:8]:
                    other = r["b_id"] if r["a_id"] == pid else r["a_id"]
                    score = float(r.get("confidence") or 0)
                    if r.get("source") == "inferred" or score < 0.5:
                        tag = "（我猜的，没验证）"
                    elif score < 0.9:
                        tag = f"（只有一方说过 {score:.2f}）"
                    else:
                        tag = f"（多方印证 {score:.2f}）"
                    lines.append(f"  - 跟 {other} 是{r['rel_type']}{tag}")
            else:
                lines.append("关系：还没记过")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[human_like_mem] who_is 失败: {e!r}")
            return "查人的时候出错了。"

    @filter.llm_tool(name="relate")
    async def relate(
        self, event: AstrMessageEvent, a: str = "", b: str = "",
        rel_type: str = "", said_by: str = "", guessed: bool = False, note: str = "",
    ):
        '''记两个人是什么关系。**单方说了不算定论**，要多方印证。

        Args:
            a(string): 一方（名字或 QQ 号）
            b(string): 另一方
            rel_type(string): 关系：家人/恋人/朋友/同事/同学/主仆/师徒/熟人/陌生人/敌对
            said_by(string): 是谁说的（名字或 QQ 号）。留空 = 主人说的
            guessed(bool): 是你自己推测出来的吗？是的话只当线索
            note(string): 原话或备注
        '''
        if self.people is None:
            return "人库还没准备好。"
        valid = ("家人", "恋人", "朋友", "同事", "同学", "主仆", "师徒", "熟人", "陌生人", "敌对")
        rt = str(rel_type or "").strip()
        if rt not in valid:
            return f"关系只能是：{'/'.join(valid)}"
        if not a or not b:
            return "要告诉我两个人分别是谁。"
        try:
            a_id = await self.people.resolve(name=str(a), create_if_missing=True)
            b_id = await self.people.resolve(name=str(b), create_if_missing=True)
            if not a_id or not b_id or a_id == b_id:
                return "这两个人没分清，先不记。"

            # 判断是谁说的 → 决定这条证据的分量
            speaker = str(said_by or "").strip()
            sender = str(event.get_sender_id() or "")
            owner = self.owner_id
            if guessed:
                kind, speaker = "inferred", "self"
            elif not speaker or speaker == owner or sender == owner:
                kind, speaker = "owner", "owner"
            else:
                # 说的人是当事人之一吗？
                pid = await self.people.resolve(name=speaker, create_if_missing=False)
                kind = "party" if pid in (a_id, b_id) else "other"
                speaker = pid or speaker

            res = await self.people.verify_relation(
                a_id, b_id, rt, speaker=speaker, speaker_kind=kind,
                quote=str(note or ""),
            )
            if not res.get("ok"):
                return f"没记上：{res.get('reason')}"

            status_cn = {
                "single": "只有一方说过，不算定论",
                "confirmed": "多方印证",
                "conflicted": "跟已有的说法矛盾，先存疑",
            }.get(res["status"], res["status"])
            return (
                f"记下了：{a} 和 {b} 是{rt}。"
                f"信任度 {res['confidence']}（{res['source_count']} 个来源，{status_cn}）"
            )
        except Exception as e:
            logger.warning(f"[human_like_mem] relate 失败: {e!r}")
            return "记关系的时候出错了。"


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

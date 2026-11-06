"""Human-like Memory · SQLite 存储。

两张表：
  memories  长期记忆（一次对话总结一条）
  atoms     原子（从记忆里拆出来的细粒度事实，带性质与生命周期）

原子另建 FTS5 索引，召回就靠它（不引入向量库，省掉 Ollama / faiss 依赖）。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from astrbot.api import logger

from .models import Atom, AtomStatus, Memory


def _norm(text: str) -> str:
    """归一化用于查重：去掉空白和标点。"""
    s = str(text or "").strip().lower()
    return re.sub(r"[\s，。、；：！？,.;:!?\"'（）()\[\]【】]+", "", s)


def _similar(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.95
    return difflib.SequenceMatcher(None, na, nb).ratio()


class MemoryStore:
    """记忆存储。所有方法都是 async，内部用 aiosqlite。"""

    def __init__(self, db_path: str, config: dict | None = None):
        self.db_path = db_path
        self.config = config or {}
        # 语义检索（可选）：由插件初始化时注入，没注入就纯关键词
        self.vector_index = None
        self.embedding_client = None

    # ---------- 配置 ----------

    @property
    def staging_enabled(self) -> bool:
        return bool(self.config.get("staging_enabled", True))

    @property
    def staging_ttl_days(self) -> float:
        hours = float(self.config.get("staging_ttl_hours", 48) or 48)
        return max(1.0 / 24.0, hours / 24.0)

    @property
    def similar_threshold(self) -> float:
        return float(self.config.get("staging_similarity_threshold", 0.85) or 0.85)

    @property
    def drop_threshold(self) -> float:
        raw = self.config.get("importance_drop_threshold", 0.3)
        # 注意：不能用 `raw or 0.3`——0.0 是 falsy，会被错误地当成 0.3
        return 0.3 if raw is None else float(raw)

    @property
    def dormant_keep_days(self) -> float:
        return float(self.config.get("dormant_keep_days", 7) or 7)

    @property
    def promote_min_reinforce(self) -> int:
        return max(1, int(self.config.get("promote_min_reinforce", 2) or 2))

    @property
    def promote_min_conf(self) -> float:
        return float(self.config.get("promote_min_confidence", 0.8) or 0.8)

    # ---------- 连接 ----------

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 10000")
            db.row_factory = aiosqlite.Row
            yield db
        finally:
            await db.close()

    # ---------- 建表 ----------

    async def initialize(self) -> None:
        import os

        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with self._connect() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    personality_summary TEXT DEFAULT '',
                    key_facts TEXT DEFAULT '[]',
                    topics TEXT DEFAULT '[]',
                    participants TEXT DEFAULT '[]',
                    participant_identities TEXT DEFAULT '[]',
                    sentiment TEXT DEFAULT 'neutral',
                    importance REAL DEFAULT 0.5,
                    session_id TEXT DEFAULT '',
                    persona_id TEXT DEFAULT '',
                    source_session_id TEXT DEFAULT '',
                    is_group INTEGER DEFAULT 0,
                    create_time REAL NOT NULL,
                    status TEXT DEFAULT 'active',
                    metadata TEXT DEFAULT '{}'
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS atoms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    layer TEXT DEFAULT '',
                    mtype TEXT DEFAULT '',
                    subtype TEXT DEFAULT '',
                    classify_conf REAL DEFAULT 0,
                    nature TEXT DEFAULT '',
                    nature_conf REAL DEFAULT 0,
                    nature_source TEXT DEFAULT '',
                    who_id TEXT DEFAULT '',
                    who_name TEXT DEFAULT '',
                    said_at REAL DEFAULT 0,
                    context TEXT DEFAULT '[]',
                    source TEXT DEFAULT '',
                    with_owner INTEGER DEFAULT 0,
                    partition_key TEXT DEFAULT '',
                    topic TEXT DEFAULT '',
                    privacy TEXT DEFAULT '',
                    status TEXT DEFAULT 'candidate',
                    importance REAL DEFAULT 0.5,
                    create_time REAL NOT NULL,
                    last_access REAL NOT NULL,
                    last_reinforce REAL DEFAULT 0,
                    reinforce_count INTEGER DEFAULT 0,
                    ttl_days REAL DEFAULT 2,
                    expires_at REAL NOT NULL,
                    session_id TEXT DEFAULT '',
                    persona_id TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}'
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_time ON memories(create_time)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_session ON memories(session_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atom_memory ON atoms(memory_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atom_status ON atoms(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atom_who ON atoms(who_id, create_time)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atom_partition ON atoms(partition_key)"
            )
            await db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS atoms_fts "
                "USING fts5(content, atom_id UNINDEXED, tokenize='unicode61')"
            )
            await db.commit()
        logger.info(f"[human_like_mem] 存储已就绪: {self.db_path}")

    # ---------- 写：长期记忆 ----------

    async def add_memory(self, mem: Memory) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO memories (
                    content, summary, personality_summary, key_facts, topics,
                    participants, participant_identities, sentiment, importance,
                    session_id, persona_id, source_session_id, is_group,
                    create_time, status, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mem.content, mem.summary, mem.personality_summary,
                    json.dumps(mem.key_facts, ensure_ascii=False),
                    json.dumps(mem.topics, ensure_ascii=False),
                    json.dumps(mem.participants, ensure_ascii=False),
                    json.dumps(mem.participant_identities, ensure_ascii=False),
                    mem.sentiment, mem.importance, mem.session_id,
                    mem.persona_id, mem.source_session_id, 1 if mem.is_group else 0,
                    mem.create_time, mem.status,
                    json.dumps(mem.metadata, ensure_ascii=False),
                ),
            )
            mem.memory_id = int(cursor.lastrowid)
            await db.commit()
        return mem.memory_id

    # ---------- 写：原子 ----------

    def _should_drop(self, atom: Atom) -> bool:
        if not self.staging_enabled:
            return False
        if atom.metadata.get("pinned"):
            return False
        th = self.drop_threshold
        if th <= 0:
            return False  # 阈值设成 0 = 明确不要丢（迁移时用）
        return float(atom.importance or 0) < th

    async def _resolve_partition(self, db, atom: Atom) -> str:
        """群聊按时间间隔、私聊按话题算分区键。"""
        if atom.partition_key:
            return atom.partition_key
        who = atom.who_id or atom.session_id or "anon"
        when = float(atom.said_at or atom.create_time or time.time())
        day = time.strftime("%Y%m%d", time.localtime(when))
        if atom.source == "group":
            gap = max(60.0, float(self.config.get("group_partition_gap_minutes", 15) or 15) * 60)
            cursor = await db.execute(
                "SELECT partition_key, said_at FROM atoms "
                "WHERE who_id = ? AND source = 'group' AND said_at > 0 "
                "ORDER BY said_at DESC LIMIT 1",
                (who,),
            )
            row = await cursor.fetchone()
            if row and row["partition_key"] and (when - float(row["said_at"] or 0)) < gap:
                return str(row["partition_key"])
            return f"g:{who}:{day}:{int((when % 86400) // gap)}"
        topic = (atom.topic or "").strip() or "misc"
        return f"p:{who}:{topic}:{day}"

    async def add_atoms(self, atoms: list[Atom], enable_dedup: bool = True) -> list[int]:
        """批量写原子。会做三件事：丢弃低价值、暂存区查重、定 TTL。"""
        if not atoms:
            return []
        kept: list[Atom] = []
        for atom in atoms:
            if self._should_drop(atom):
                logger.info(
                    f"[human_like_mem] 重要性 {atom.importance:.2f} 低于阈值，丢弃: {atom.content[:40]}"
                )
                continue
            kept.append(atom)
        if not kept:
            return []

        now = time.time()
        ids: list[int] = []
        async with self._connect() as db:
            for atom in kept:
                # 暂存区查重：同一件事换个说法别重复记
                if enable_dedup and self.staging_enabled:
                    hit = await self._find_similar(db, atom)
                    if hit:
                        await self._reinforce(db, hit[0], atom.nature_conf or atom.classify_conf)
                        atom.atom_id = hit[0]
                        ids.append(hit[0])
                        logger.info(
                            f"[human_like_mem] 暂存命中(相似 {hit[1]:.2f})，强化不新建: {atom.content[:30]}"
                        )
                        continue

                atom.partition_key = atom.partition_key or await self._resolve_partition(db, atom)
                if self.staging_enabled and atom.status == AtomStatus.CANDIDATE.value:
                    atom.ttl_days = self.staging_ttl_days
                else:
                    atom.ttl_days = atom.compute_ttl()
                atom.expires_at = now + atom.ttl_days * 86400.0
                atom.create_time = atom.create_time or now
                atom.last_access = now

                cursor = await db.execute(
                    """
                    INSERT INTO atoms (
                        memory_id, content, layer, mtype, subtype, classify_conf,
                        nature, nature_conf, nature_source, who_id, who_name,
                        said_at, context, source, with_owner, partition_key, topic,
                        privacy, status, importance, create_time, last_access,
                        last_reinforce, reinforce_count, ttl_days, expires_at,
                        session_id, persona_id, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        atom.memory_id, atom.content, atom.layer, atom.mtype,
                        atom.subtype, atom.classify_conf, atom.nature,
                        atom.nature_conf, atom.nature_source, atom.who_id,
                        atom.who_name, atom.said_at,
                        json.dumps(atom.context or [], ensure_ascii=False),
                        atom.source, 1 if atom.with_owner else 0,
                        atom.partition_key, atom.topic, atom.privacy,
                        atom.status, atom.importance, atom.create_time,
                        atom.last_access, atom.last_reinforce,
                        atom.reinforce_count, atom.ttl_days, atom.expires_at,
                        atom.session_id, atom.persona_id,
                        json.dumps(atom.metadata, ensure_ascii=False),
                    ),
                )
                aid = int(cursor.lastrowid)
                atom.atom_id = aid
                ids.append(aid)
                await db.execute(
                    "INSERT INTO atoms_fts(atom_id, content) VALUES (?, ?)",
                    (aid, atom.content),
                )
            await db.commit()
        return ids

    async def _find_similar(self, db, atom: Atom) -> tuple[int, float] | None:
        """在暂存区里找相似的原子。"""
        cursor = await db.execute(
            "SELECT id, content FROM atoms WHERE status = ? AND session_id = ? "
            "ORDER BY create_time DESC LIMIT 40",
            (AtomStatus.CANDIDATE.value, atom.session_id),
        )
        rows = await cursor.fetchall()
        best_id, best = 0, 0.0
        for row in rows:
            score = _similar(atom.content, row["content"])
            if score > best:
                best_id, best = int(row["id"]), score
        if best_id and best >= self.similar_threshold:
            return best_id, best
        return None

    async def _reinforce(self, db, atom_id: int, new_conf: float = 0.0) -> None:
        """强化一条原子；够条件就转正。"""
        now = time.time()
        cursor = await db.execute(
            "SELECT reinforcement_count, confidence, atom_type, mtype, importance, "
            "status, nature, nature_conf FROM atoms WHERE id = ?",
            (atom_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return
        count = int(row["reinforcement_count"]) + 1
        conf = float(row["nature_conf"] or 0)
        if new_conf:
            conf = conf * 0.7 + new_conf * 0.3
        status = str(row["status"] or "")
        nature = str(row["nature"] or "")
        if status == AtomStatus.CANDIDATE.value:
            promote = count >= self.promote_min_reinforce or (
                nature and nature != "joke_guess" and conf >= self.promote_min_conf
            )
            if promote:
                status = AtomStatus.ACTIVE.value
                logger.info(f"[human_like_mem] 暂存转正(次数 {count}, 置信 {conf:.2f})")
        if status == AtomStatus.CANDIDATE.value:
            ttl = self.staging_ttl_days
        else:
            from .models import BASE_TTL

            base = BASE_TTL.get(str(row["mtype"] or "unknown"), 30)
            imp = float(row["importance"] or 0.5)
            ttl = max(1.0, base * (0.5 + imp) * (1 + min(0.5, count * 0.1)))
        await db.execute(
            "UPDATE atoms SET reinforcement_count = ?, nature_conf = ?, status = ?, "
            "ttl_days = ?, expires_at = ?, last_reinforce = ? WHERE id = ?",
            (count, conf, status, ttl, now + ttl * 86400.0, now, atom_id),
        )

    # ---------- 读：检索 ----------

    async def search_atoms(
        self,
        query: str,
        limit: int = 5,
        session_id: str | None = None,
    ) -> list[Atom]:
        """找跟查询最相关的记忆。

        两路召回：
          关键词路：带权检索词（停用词过滤 + 同义词扩展）LIKE 粗筛 → 打分
          语义路  ：查询向量 vs 已建好的原子向量（没索引就自动跳过）
        两路归一后加权合并，再去重取前 N。
        """
        from .retrieval import extract_terms, rerank, score_content
        from .semantic import hybrid_rank

        terms = extract_terms(query)
        if not terms:
            return []

        # ---------- 关键词路 ----------
        words = [t for t, _ in terms]
        conds = " OR ".join("content LIKE ?" for _ in words)
        sql = (
            "SELECT * FROM atoms "
            f"WHERE status IN ('active','candidate','dormant') AND ({conds}) "
            "ORDER BY last_access DESC LIMIT ?"
        )
        params: list[Any] = [f"%{w}%" for w in words] + [max(30, limit * 10)]
        kw_scores: dict[int, float] = {}
        candidates: dict[int, Atom] = {}
        try:
            async with self._connect() as db:
                cursor = await db.execute(sql, params)
                rows = await cursor.fetchall()
            for row in rows:
                atom = self._row_to_atom(row)
                s, hits, max_w = score_content(atom.content, terms)
                if hits == 0 or max_w < 0.8:
                    continue
                if atom.status == AtomStatus.DORMANT.value:
                    s *= 0.3
                elif atom.status == AtomStatus.CANDIDATE.value:
                    s *= float(self.config.get("staging_recall_penalty", 0.5) or 0.5)
                s *= max(0.1, float(atom.importance or 0.5))
                kw_scores[atom.atom_id] = s
                candidates[atom.atom_id] = atom
        except Exception as e:
            logger.warning(f"[human_like_mem] 关键词检索失败: {e!r}")

        # ---------- 语义路 ----------
        vec_scores: dict[int, float] = {}
        if self.vector_index is not None and self.embedding_client is not None:
            try:
                qvec = await self.embedding_client.embed(query)
                if qvec:
                    for aid, sim in self.vector_index.search(qvec, top_k=max(20, limit * 6)):
                        vec_scores[int(aid)] = float(sim)
            except Exception as e:
                logger.debug(f"[human_like_mem] 语义检索失败: {e!r}")

        # 语义召回里有关键词没覆盖到的，补查出来
        extra_ids = [aid for aid in vec_scores if aid not in candidates]
        if extra_ids:
            try:
                ph = ",".join("?" * len(extra_ids))
                async with self._connect() as db:
                    cursor = await db.execute(
                        f"SELECT * FROM atoms WHERE id IN ({ph}) "
                        "AND status IN ('active','candidate','dormant')",
                        extra_ids,
                    )
                    for row in await cursor.fetchall():
                        atom = self._row_to_atom(row)
                        candidates[atom.atom_id] = atom
            except Exception as e:
                logger.debug(f"[human_like_mem] 补查语义候选失败: {e!r}")

        if not candidates:
            return []

        # ---------- 合并 ----------
        if vec_scores:
            ranked = hybrid_rank(kw_scores, vec_scores)
            ordered = [candidates[aid] for aid, _ in ranked if aid in candidates]
        else:
            ordered = list(candidates.values())

        # 最后按内容去重（同一件事的多个说法只留一条）。
        # 有语义路参与时不再卡「强词」——语义召回的句子本来就不含查询原词。
        has_vector = bool(vec_scores)
        return rerank(
            ordered,
            query,
            dormant_penalty=0.3,
            candidate_penalty=float(self.config.get("staging_recall_penalty", 0.5) or 0.5),
            limit=limit,
            min_weight=0.0 if has_vector else 0.8,
        )

    async def hot_atoms(
        self,
        query: str = "",
        hours: float = 48,
        limit: int = 3,
    ) -> list[Atom]:
        """热记忆：最近动过的正式记忆；有查询时按相关性挑，而不是单纯按时间。"""
        from .retrieval import extract_terms, rerank

        cutoff = time.time() - hours * 3600.0
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM atoms WHERE status = ? AND last_access >= ? "
                "ORDER BY last_access DESC LIMIT ?",
                (AtomStatus.ACTIVE.value, cutoff, max(10, limit * 6)),
            )
            rows = await cursor.fetchall()
        if not rows:
            return []
        atoms = [self._row_to_atom(r) for r in rows]

        q = str(query or "").strip()
        if q and extract_terms(q):
            picked = rerank(atoms, q, dormant_penalty=0.3, candidate_penalty=0.5, limit=limit)
            if picked:
                return picked
        # 没有查询或没匹配上：退回「最近动过的」
        return atoms[:limit]

    async def recent_atoms(self, hours: float = 48, limit: int = 3) -> list[Atom]:
        """最近动过的正式记忆（不带查询时的简易版）。"""
        return await self.hot_atoms(query="", hours=hours, limit=limit)

    async def recent_atoms(self, hours: float = 48, limit: int = 3) -> list[Atom]:
        """最近动过的正式记忆（热记忆）。"""
        cutoff = time.time() - hours * 3600.0
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM atoms WHERE status = ? AND last_access >= ? "
                "ORDER BY last_access DESC LIMIT ?",
                (AtomStatus.ACTIVE.value, cutoff, max(1, limit)),
            )
            rows = await cursor.fetchall()
        return [self._row_to_atom(r) for r in rows]

    # ---------- 回溯比对 ----------

    @staticmethod
    def _fragments(text: str, max_pieces: int = 6) -> list[str]:
        s = str(text or "").strip()
        out: list[str] = []
        for i in range(max(0, len(s) - 1)):
            piece = s[i : i + 2]
            if len(piece) == 2 and piece.strip() and piece not in out:
                out.append(piece)
            if len(out) >= max_pieces:
                break
        return out

    async def find_related_history(
        self, who_id: str, text: str, days: float = 30, limit: int = 3
    ) -> list[dict]:
        """这个人以前说过类似的话吗？返回当时的性质判断。"""
        frags = self._fragments(text)
        if not who_id or not frags:
            return []
        cutoff = time.time() - max(1.0, days) * 86400.0
        conds = " OR ".join("content LIKE ?" for _ in frags)
        sql = (
            "SELECT id, content, nature, nature_conf, mtype FROM atoms "
            f"WHERE who_id = ? AND create_time >= ? AND nature != '' AND ({conds}) "
            "ORDER BY create_time DESC LIMIT ?"
        )
        params: list[Any] = [who_id, cutoff, *[f"%{f}%" for f in frags], limit]
        try:
            async with self._connect() as db:
                cursor = await db.execute(sql, params)
                rows = await cursor.fetchall()
        except Exception as e:
            logger.debug(f"[human_like_mem] 查历史失败: {e!r}")
            return []
        return [
            {
                "id": int(r["id"]),
                "content": str(r["content"] or "")[:80],
                "nature": str(r["nature"] or ""),
                "nature_conf": float(r["nature_conf"] or 0),
                "mtype": str(r["mtype"] or ""),
            }
            for r in rows
        ]

    # ---------- 生命周期 ----------

    async def expire_stale(self) -> dict[str, int]:
        """暂存到期 → 待定；正式到期 → 过期。"""
        now = time.time()
        keep = self.dormant_keep_days
        async with self._connect() as db:
            c1 = await db.execute(
                "UPDATE atoms SET status = ?, expires_at = ? WHERE status = ? AND expires_at < ?",
                (AtomStatus.DORMANT.value, now + keep * 86400.0,
                 AtomStatus.CANDIDATE.value, now),
            )
            c2 = await db.execute(
                "UPDATE atoms SET status = ? WHERE status = ? AND expires_at < ?",
                (AtomStatus.EXPIRED.value, AtomStatus.ACTIVE.value, now),
            )
            await db.commit()
            n1, n2 = c1.rowcount, c2.rowcount
        if n1:
            logger.info(f"[human_like_mem] {n1} 条暂存转待定（{keep:.0f} 天后删）")
        return {"dormant": n1, "expired": n2}

    async def purge_dormant(self) -> int:
        """待定超过保留期 → 真删。"""
        now = time.time()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id FROM atoms WHERE status = ? AND expires_at < ?",
                (AtomStatus.DORMANT.value, now),
            )
            rows = await cursor.fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                ph = ",".join("?" * len(ids))
                await db.execute(f"DELETE FROM atoms_fts WHERE atom_id IN ({ph})", ids)
                await db.execute(f"DELETE FROM atoms WHERE id IN ({ph})", ids)
                await db.commit()
        if ids:
            logger.info(f"[human_like_mem] 清理 {len(ids)} 条待定原子")
        return len(ids)

    # ---------- 问主人 ----------

    async def find_pending_asks(self, limit: int = 5) -> list[Atom]:
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT * FROM atoms WHERE json_extract(metadata, '$.ask_owner') = 1 "
                    "AND COALESCE(json_extract(metadata, '$.asked_owner'), 0) = 0 "
                    "AND status IN ('candidate','active') "
                    "ORDER BY create_time DESC LIMIT ?",
                    (limit,),
                )
                rows = await cursor.fetchall()
        except Exception as e:
            logger.warning(f"[human_like_mem] 查待问失败: {e!r}")
            return []
        return [self._row_to_atom(r) for r in rows]

    async def mark_asked(self, ids: list[int]) -> int:
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        async with self._connect() as db:
            await db.execute(
                f"UPDATE atoms SET metadata = json_set(metadata, '$.asked_owner', 1) "
                f"WHERE id IN ({ph})",
                ids,
            )
            await db.commit()
        return len(ids)

    # ---------- 红线 ----------

    async def sensitive_memory_ids(self, limit: int = 500) -> set[int]:
        """标了「私密性」的记忆 id（红线第二档）。"""
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT DISTINCT memory_id FROM atoms "
                    "WHERE privacy = ? AND memory_id > 0 LIMIT ?",
                    ("sensitive", limit),
                )
                rows = await cursor.fetchall()
            return {int(r["memory_id"]) for r in rows if r["memory_id"]}
        except Exception as e:
            logger.debug(f"[human_like_mem] 查私密失败: {e!r}")
            return set()

    # ---------- 纠正 ----------

    async def update_nature(self, atom_id: int, nature: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE atoms SET nature = ?, nature_conf = 1.0, nature_source = 'owner' "
                "WHERE id = ?",
                (nature, atom_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_atom(self, atom_id: int) -> Atom | None:
        async with self._connect() as db:
            cursor = await db.execute("SELECT * FROM atoms WHERE id = ?", (atom_id,))
            row = await cursor.fetchone()
        return self._row_to_atom(row) if row else None

    async def touch(self, atom_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE atoms SET last_access = ? WHERE id = ?", (time.time(), atom_id)
            )
            await db.commit()

    # ---------- 统计 ----------

    async def get_stats(self) -> dict[str, Any]:
        async with self._connect() as db:
            c1 = await db.execute("SELECT COUNT(*) FROM memories")
            mem_n = int((await c1.fetchone())[0])
            c2 = await db.execute("SELECT status, COUNT(*) FROM atoms GROUP BY status")
            rows = await c2.fetchall()
        return {
            "memories": mem_n,
            "atoms": {str(r[0]): int(r[1]) for r in rows},
        }

    # ---------- 行 → 对象 ----------

    def _row_to_atom(self, row) -> Atom:
        def g(key, default=None):
            try:
                return row[key]
            except (IndexError, KeyError):
                return default

        try:
            ctx = json.loads(g("context") or "[]")
        except Exception:
            ctx = []
        try:
            meta = json.loads(g("metadata") or "{}")
        except Exception:
            meta = {}
        return Atom(
            atom_id=int(g("id", 0) or 0),
            memory_id=int(g("memory_id", 0) or 0),
            content=str(g("content") or ""),
            layer=str(g("layer") or ""),
            mtype=str(g("mtype") or ""),
            subtype=str(g("subtype") or ""),
            classify_conf=float(g("classify_conf") or 0),
            nature=str(g("nature") or ""),
            nature_conf=float(g("nature_conf") or 0),
            nature_source=str(g("nature_source") or ""),
            who_id=str(g("who_id") or ""),
            who_name=str(g("who_name") or ""),
            said_at=float(g("said_at") or 0),
            context=ctx if isinstance(ctx, list) else [],
            source=str(g("source") or ""),
            with_owner=bool(g("with_owner") or 0),
            partition_key=str(g("partition_key") or ""),
            topic=str(g("topic") or ""),
            privacy=str(g("privacy") or ""),
            status=str(g("status") or ""),
            importance=float(g("importance") or 0.5),
            create_time=float(g("create_time") or 0),
            last_access=float(g("last_access") or 0),
            last_reinforce=float(g("last_reinforce") or 0),
            reinforce_count=int(g("reinforce_count") or 0),
            ttl_days=float(g("ttl_days") or 0),
            expires_at=float(g("expires_at") or 0),
            session_id=str(g("session_id") or ""),
            persona_id=str(g("persona_id") or ""),
            metadata=meta if isinstance(meta, dict) else {},
        )


__all__ = ["MemoryStore"]

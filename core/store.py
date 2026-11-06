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


def _json_list(raw) -> list[str]:
    """解析 JSON 数组字段，坏了就当空。"""
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []


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

    @property
    def promote_by_importance(self) -> float:
        """重要性到这个值，不用等定性也能转正。"""
        raw = self.config.get("promote_by_importance", 0.65)
        return 0.65 if raw is None else float(raw)

    @property
    def promote_fact_conf(self) -> float:
        """陈述/承诺类专用的低门槛。

        fact 是「一件事发生了」，本来就不需要判「是玩笑还是真骂」。
        硬要求它跟 joke/scold 一样到 0.8，等于让陈述句先证明自己是陈述句——
        这就是大部分记忆永远转不正的原因。
        """
        raw = self.config.get("promote_fact_confidence", 0.55)
        return 0.55 if raw is None else float(raw)

    def should_promote(
        self,
        *,
        mtype: str,
        nature_conf: float,
        importance: float,
        reinforce_count: int,
        nature: str = "",
    ) -> bool:
        """能不能从暂存区转正。四个口子，任一满足。"""
        # ① 被反复提到
        if reinforce_count >= self.promote_min_reinforce:
            return True
        # ② 定性够确定
        if nature_conf >= self.promote_min_conf:
            return True
        # ③ 陈述 / 承诺 / 夸奖类：不靠"判性质"也能进
        #    夸奖只是记下一句好话，判错代价很小，没必要跟"真骂"用同一个门槛
        if mtype in ("fact", "promise", "praise") and nature_conf >= self.promote_fact_conf:
            return True
        # ④ 事情本身重要
        if float(importance or 0) >= self.promote_by_importance:
            return True
        return False

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
                    person_ids TEXT DEFAULT '[]',
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
            # 老库迁移：这些列是后来加上的，CREATE TABLE IF NOT EXISTS 不会补
            _new_cols = {
                "layer": "TEXT DEFAULT ''",
                "mtype": "TEXT DEFAULT ''",
                "subtype": "TEXT DEFAULT ''",
                "classify_conf": "REAL DEFAULT 0",
                "nature": "TEXT DEFAULT ''",
                "nature_conf": "REAL DEFAULT 0",
                "nature_source": "TEXT DEFAULT ''",
                "who_id": "TEXT DEFAULT ''",
                "who_name": "TEXT DEFAULT ''",
                "person_ids": "TEXT DEFAULT '[]'",
                "said_at": "REAL DEFAULT 0",
                "context": "TEXT DEFAULT '[]'",
                "source": "TEXT DEFAULT ''",
                "with_owner": "INTEGER DEFAULT 0",
                "partition_key": "TEXT DEFAULT ''",
                "topic": "TEXT DEFAULT ''",
                "privacy": "TEXT DEFAULT ''",
            }
            cur = await db.execute("PRAGMA table_info(atoms)")
            existing = {row[1] for row in await cur.fetchall()}
            added = []
            for _col, _decl in _new_cols.items():
                if _col not in existing:
                    await db.execute(f"ALTER TABLE atoms ADD COLUMN {_col} {_decl}")
                    added.append(_col)
            if added:
                logger.info(f"[human_like_mem] 老库迁移：补列 {added}")
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

    async def _recover_from_archive(self, db, atom: Atom) -> int:
        """同一件事又被提起了：从遗忘库把它拉回来。

        返回被拉起的 atom_id（0 = 没有相似的）。拉起来之后回到暂存区，
        重新走一遍「定性 → 转正」，而不是直接恢复成正式记忆——
        因为这次提起的语境可能不一样了。
        """
        if not bool(self.config.get("recover_on_repeat", True)):
            return 0
        frags = self._fragments(atom.content, max_pieces=5)
        if not frags:
            return 0
        conds = " OR ".join("content LIKE ?" for _ in frags)
        try:
            cursor = await db.execute(
                f"SELECT id, content FROM atoms WHERE status = ? AND ({conds}) LIMIT 80",
                [AtomStatus.ARCHIVED.value, *[f"%{f}%" for f in frags]],
            )
            rows = await cursor.fetchall()
        except Exception as e:
            logger.debug(f"[human_like_mem] 查遗忘库失败: {e!r}")
            return 0

        best_id, best = 0, 0.0
        for row in rows:
            score = _similar(atom.content, str(row["content"] or ""))
            if score > best:
                best_id, best = int(row["id"]), score
        # 遗忘库的拉起阈值要比暂存区宽松：隔了很久的事，说法肯定不一样
        # （「养过一只叫小黑的狗」vs「养的那只狗叫小黑」）
        threshold = self.recover_threshold
        if not best_id or best < threshold:
            return 0

        now = time.time()
        try:
            await db.execute(
                "UPDATE atoms SET status = ?, expires_at = ?, "
                "reinforce_count = reinforce_count + 1, last_reinforce = ?, "
                "last_access = ?, "
                "metadata = json_set(COALESCE(metadata,'{}'), '$.recovered_at', ?) "
                "WHERE id = ?",
                (
                    AtomStatus.CANDIDATE.value,
                    now + self.staging_ttl_days * 86400.0,
                    now,
                    now,
                    now,
                    best_id,
                ),
            )
        except Exception as e:
            logger.warning(f"[human_like_mem] 拉起遗忘记忆失败: {e!r}")
            return 0
        logger.info(
            f"[human_like_mem] 同一件事又被提起(相似 {best:.2f})，从遗忘库拉起 #{best_id}: "
            f"{atom.content[:40]}"
        )
        return best_id

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
                    # 暂存区没有 → 看看是不是遗忘库里躺着的旧事
                    recovered = await self._recover_from_archive(db, atom)
                    if recovered:
                        atom.atom_id = recovered
                        ids.append(recovered)
                        continue

                atom.partition_key = atom.partition_key or await self._resolve_partition(db, atom)
                if self.staging_enabled and atom.status == AtomStatus.CANDIDATE.value:
                    # 写入时就判一次能不能转正：
                    # 重要的事、陈述/承诺类、定性确定的，不该在暂存区干等着——
                    # 大部分话这辈子只说一次，等不到"被提第二遍"。
                    if self.should_promote(
                        mtype=str(atom.mtype or ""),
                        nature_conf=float(atom.nature_conf or 0),
                        importance=float(atom.importance or 0),
                        reinforce_count=0,
                        nature=str(atom.nature or ""),
                    ):
                        atom.status = AtomStatus.ACTIVE.value
                        atom.ttl_days = atom.compute_ttl()
                        logger.debug(
                            f"[human_like_mem] 入段即转正（{atom.mtype} "
                            f"重要性 {atom.importance:.2f}）：{atom.content[:30]}"
                        )
                    else:
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
                        nature, nature_conf, nature_source, who_id, who_name, person_ids,
                        said_at, context, source, with_owner, partition_key, topic,
                        privacy, status, importance, create_time, last_access,
                        last_reinforce, reinforce_count, ttl_days, expires_at,
                        session_id, persona_id, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        atom.memory_id, atom.content, atom.layer, atom.mtype,
                        atom.subtype, atom.classify_conf, atom.nature,
                        atom.nature_conf, atom.nature_source, atom.who_id,
                        atom.who_name,
                        json.dumps(atom.person_ids or [], ensure_ascii=False),
                        atom.said_at,
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
        """找相似的已有原子。

        以前只在**暂存区**里找，结果「重复转正」这条路从没跑起来过——
        因为写入时常常直接转正了，它就不在暂存区了，下一句类似的话找不到它。
        现在 candidate 和 active 都找（只查最近 90 天，避免全库扫）。
        """
        cursor = await db.execute(
            "SELECT id, content FROM atoms "
            "WHERE status IN (?, ?) AND session_id = ? AND create_time > ? "
            "ORDER BY create_time DESC LIMIT 60",
            (
                AtomStatus.CANDIDATE.value,
                AtomStatus.ACTIVE.value,
                atom.session_id,
                time.time() - 90 * 86400.0,
            ),
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

    async def promote_candidates(self, limit: int = 300) -> int:
        """定时重判：扫一遍暂存区，够条件的转正。

        以前 candidate 只在两个时刻有转正机会：
          1. 刚写入时瞬判一次
          2. 被同一件事再次提起
        大部分话这辈子只说一次，所以会一直卡着干等到期（48 小时→待定）。
        维护循环里定期扫一遍就不会卡了。
        """
        now = time.time()
        promoted = 0
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, mtype, nature, nature_conf, importance, reinforce_count "
                "FROM atoms WHERE status = ? LIMIT ?",
                (AtomStatus.CANDIDATE.value, max(1, int(limit))),
            )
            rows = await cursor.fetchall()
            for r in rows:
                if not self.should_promote(
                    mtype=str(r["mtype"] or ""),
                    nature_conf=float(r["nature_conf"] or 0),
                    importance=float(r["importance"] or 0),
                    reinforce_count=int(r["reinforce_count"] or 0),
                    nature=str(r["nature"] or ""),
                ):
                    continue
                from .models import BASE_TTL

                base = BASE_TTL.get(str(r["mtype"] or "unknown"), 30)
                imp = float(r["importance"] or 0.5)
                ttl = max(1.0, base * (0.5 + imp))
                await db.execute(
                    "UPDATE atoms SET status = ?, ttl_days = ?, expires_at = ? WHERE id = ?",
                    (AtomStatus.ACTIVE.value, ttl, now + ttl * 86400.0, int(r["id"])),
                )
                promoted += 1
            if promoted:
                await db.commit()
        if promoted:
            logger.info(f"[human_like_mem] 定时重判：{promoted} 条暂存转正")
        return promoted

    async def _reinforce(self, db, atom_id: int, new_conf: float = 0.0) -> None:
        """强化一条原子；够条件就转正。"""
        now = time.time()
        cursor = await db.execute(
            "SELECT reinforce_count, mtype, importance, "
            "status, nature, nature_conf FROM atoms WHERE id = ?",
            (atom_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return
        count = int(row["reinforce_count"] or 0) + 1
        conf = float(row["nature_conf"] or 0)
        if new_conf:
            conf = conf * 0.7 + new_conf * 0.3
        status = str(row["status"] or "")
        nature = str(row["nature"] or "")
        if status == AtomStatus.CANDIDATE.value:
            promote = self.should_promote(
                mtype=str(row["mtype"] or ""),
                nature_conf=conf,
                importance=float(row["importance"] or 0),
                reinforce_count=count,
                nature=nature,
            )
            if promote:
                status = AtomStatus.ACTIVE.value
                logger.info(
                    f"[human_like_mem] 暂存转正(次数 {count}, 置信 {conf:.2f}, "
                    f"类型 {row['mtype']}, 重要性 {row['importance']:.2f})"
                )
        if status == AtomStatus.CANDIDATE.value:
            ttl = self.staging_ttl_days
        else:
            from .models import BASE_TTL

            base = BASE_TTL.get(str(row["mtype"] or "unknown"), 30)
            imp = float(row["importance"] or 0.5)
            ttl = max(1.0, base * (0.5 + imp) * (1 + min(0.5, count * 0.1)))
        await db.execute(
            "UPDATE atoms SET reinforce_count = ?, nature_conf = ?, status = ?, "
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
        """生命周期推进。

        - 暂存到期没定性 → 待定（dormant）
        - 正式记忆到期      → 沉入遗忘库（archived），不检索，但会被再次提起唤醒
        """
        now = time.time()
        keep = self.dormant_keep_days
        archive_keep = self.archive_keep_days
        async with self._connect() as db:
            c1 = await db.execute(
                "UPDATE atoms SET status = ?, expires_at = ? WHERE status = ? AND expires_at < ?",
                (AtomStatus.DORMANT.value, now + keep * 86400.0,
                 AtomStatus.CANDIDATE.value, now),
            )
            # 正式记忆到期 → 遗忘库，给一个新的「彻底删掉」的期限
            c2 = await db.execute(
                "UPDATE atoms SET status = ?, expires_at = ?, "
                "metadata = json_set(COALESCE(metadata,'{}'), '$.archived_at', ?) "
                "WHERE status = ? AND expires_at < ?",
                (AtomStatus.ARCHIVED.value, now + archive_keep * 86400.0,
                 now, AtomStatus.ACTIVE.value, now),
            )
            await db.commit()
            n1, n2 = c1.rowcount, c2.rowcount
        if n1:
            logger.info(f"[human_like_mem] {n1} 条暂存转待定（{keep:.0f} 天后删）")
        if n2:
            logger.info(f"[human_like_mem] {n2} 条正式记忆沉入遗忘库（{archive_keep:.0f} 天后真删）")
        return {"dormant": n1, "archived": n2}

    @property
    def archive_keep_days(self) -> float:
        """遗忘库保留多少天（之后彻底删掉）。"""
        raw = self.config.get("archive_keep_days", 180)
        return 180.0 if raw is None else float(raw)

    @property
    def recover_threshold(self) -> float:
        """遗忘库拉起阈值。比暂存区查重宽松：隔了很久的说法差别大。"""
        raw = self.config.get("recover_similarity_threshold", 0.62)
        return 0.62 if raw is None else float(raw)

    async def purge_archived(self) -> int:
        """遗忘库放太久的，彻底删掉。"""
        now = time.time()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id FROM atoms WHERE status = ? AND expires_at < ?",
                (AtomStatus.ARCHIVED.value, now),
            )
            rows = await cursor.fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                ph = ",".join("?" * len(ids))
                await db.execute(f"DELETE FROM atoms_fts WHERE atom_id IN ({ph})", ids)
                await db.execute(f"DELETE FROM atoms WHERE id IN ({ph})", ids)
                # 向量表可能没建（没开语义检索时不存在）
                try:
                    await db.execute(f"DELETE FROM atom_vectors WHERE atom_id IN ({ph})", ids)
                except Exception:
                    pass
                await db.commit()
        if ids:
            logger.info(f"[human_like_mem] 遗忘库清理 {len(ids)} 条")
        return len(ids)

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
            person_ids=(_json_list(g("person_ids"))),
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

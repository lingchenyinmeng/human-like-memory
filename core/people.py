"""人际关系知识库（people.db）。

跟事件库（memory.db）物理分开：这边只记「谁是谁」「谁和谁什么关系」，
事件库那边存「谁在什么时候说了什么」，两边靠 person_id 软关联，不建外键。

三张表：
  people        一个人一个 person_id（只负责"这是同一个人"）
  person_tags   所有叫法（QQ号 / 真名 / 昵称 / 外号），带来源、时间、置信度
  relations     关系边（两个 person_id + 类型 + 生效区间 + 可信度）

设计要点：
- 一个 id 能挂一堆标签；同一个外号两个人共用是允许的（标签碰撞），靠别的维度判
- 关系是"边"不是"一句话"：有时间区间，变了就改 to_ts 再开新的，不覆盖历史
- 可信度分级：主人亲口 1.0（钉死）/ 别人说的 0.7（照收不进死档）/ 我推的 0.5（标"猜的"）
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from astrbot.api import logger

# 关系类型（无向用 mutual，有向用 a_to_b / b_to_a）
REL_TYPES: tuple[str, ...] = (
    "家人", "恋人", "朋友", "同事", "同学", "主仆", "师徒", "熟人", "陌生人", "敌对",
)

# 标签种类
TAG_KINDS: tuple[str, ...] = ("qq", "real_name", "nickname", "alias", "group_card")

# 来源与可信度
SOURCE_OWNER = "owner"        # 主人亲口 → 钉死
SOURCE_OTHER = "other"        # 别人说的 → 照收
SOURCE_INFERRED = "inferred"  # 我自己推的 → 只当线索

SOURCE_CONF = {SOURCE_OWNER: 1.0, SOURCE_OTHER: 0.7, SOURCE_INFERRED: 0.5}

# 单条证据的权重上限：任何一方单独说都不算"定论"（0.85 封顶）
EVIDENCE_WEIGHT_CAP = 0.85

# 说话人身份 → 证据权重
SPEAKER_WEIGHT = {
    "owner": 0.85,      # 主人亲口
    "party": 0.80,      # 当事人自己说的（A 说"B 是我朋友"）
    "other": 0.70,      # 第三方说的
    "inferred": 0.40,   # 我自己推的
}


def confidence_from_evidence(weights: list[float], conflicted: bool = False) -> float:
    """多向验证：由独立证据算置信度。

    规则：
    - 单条证据封顶 0.85（谁一个人说了都不算定论）
    - 独立来源越多越可信：1 - Π(1-w)
    - 说法矛盾时打对折
    """
    if not weights:
        return 0.0
    capped = [min(EVIDENCE_WEIGHT_CAP, max(0.0, float(w))) for w in weights]
    miss = 1.0
    for w in capped:
        miss *= (1.0 - w)
    conf = 1.0 - miss
    if len(capped) == 1:
        conf = min(conf, EVIDENCE_WEIGHT_CAP)
    if conflicted:
        conf *= 0.5
    return round(max(0.0, min(1.0, conf)), 3)


def evidence_status(source_count: int, conflicted: bool) -> str:
    """这条关系现在算到什么程度。"""
    if conflicted:
        return "conflicted"     # 矛盾，待判
    if source_count >= 2:
        return "confirmed"      # 多方印证
    if source_count == 1:
        return "single"         # 只有一方说过，不算定论
    return "unverified"


def _new_person_id() -> str:
    return "p_" + uuid.uuid4().hex[:12]


def _unknown_id(key: str) -> str:
    """认不出来的人：按内容 hash 生成稳定 id，同名同 hash 会落到同一个。"""
    return "p_unk_" + hashlib.md5(str(key).encode("utf-8")).hexdigest()[:10]


class PeopleStore:
    """人表 + 标签 + 关系边。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

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
                CREATE TABLE IF NOT EXISTS people (
                    person_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    note TEXT DEFAULT '',
                    is_unknown INTEGER DEFAULT 0,
                    -- 人也是一个「原子」：有活性、有生命周期，不靠关系活着
                    last_seen REAL DEFAULT 0,
                    mention_count INTEGER DEFAULT 0,
                    importance REAL DEFAULT 0.5,
                    status TEXT DEFAULT 'active'
                )
                """
            )
            # 老库迁移：people 表后加的列
            _people_cols = {
                "last_seen": "REAL DEFAULT 0",
                "mention_count": "INTEGER DEFAULT 0",
                "importance": "REAL DEFAULT 0.5",
                "status": "TEXT DEFAULT 'active'",
            }
            cur = await db.execute("PRAGMA table_info(people)")
            have = {row[1] for row in await cur.fetchall()}
            added = []
            for col, decl in _people_cols.items():
                if col not in have:
                    await db.execute(f"ALTER TABLE people ADD COLUMN {col} {decl}")
                    added.append(col)
            if added:
                logger.info(f"[human_like_mem] 人表迁移：补列 {added}")
                # 老数据的 last_seen 是 0，会被误判成「很久没出现」，补成创建时间
                await db.execute(
                    "UPDATE people SET last_seen = created_at "
                    "WHERE last_seen IS NULL OR last_seen = 0"
                )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS person_tags (
                    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT DEFAULT 'inferred',
                    confidence REAL DEFAULT 0.5,
                    seen_at REAL NOT NULL,
                    note TEXT DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS relations (
                    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    a_id TEXT NOT NULL,
                    b_id TEXT NOT NULL,
                    rel_type TEXT NOT NULL,
                    direction TEXT DEFAULT 'mutual',
                    from_ts REAL NOT NULL,
                    to_ts REAL,
                    confidence REAL DEFAULT 0.5,
                    source TEXT DEFAULT 'inferred',
                    updated_by TEXT DEFAULT '',
                    updated_at REAL NOT NULL,
                    note TEXT DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload TEXT DEFAULT '{}',
                    created_at REAL NOT NULL,
                    resolved INTEGER DEFAULT 0
                )
                """
            )
            # 关系证据链：一条关系由多条证据支撑，来源必须相互独立才算数
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS relation_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    edge_id INTEGER NOT NULL,
                    a_id TEXT NOT NULL,
                    b_id TEXT NOT NULL,
                    rel_type TEXT NOT NULL,
                    speaker TEXT DEFAULT '',
                    speaker_kind TEXT DEFAULT 'inferred',
                    quote TEXT DEFAULT '',
                    weight REAL DEFAULT 0.4,
                    created_at REAL NOT NULL
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tag_person ON person_tags(person_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tag_value ON person_tags(kind, value)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_rel_a ON relations(a_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_rel_b ON relations(b_id)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ev_edge ON relation_evidence(edge_id)"
            )
            await db.commit()
        logger.info(f"[human_like_mem] 人表已就绪: {self.db_path}")

    # ---------- 人 ----------

    async def create_person(self, note: str = "", is_unknown: bool = False) -> str:
        pid = _new_person_id()
        async with self._connect() as db:
            now = time.time()
            await db.execute(
                "INSERT INTO people(person_id, created_at, note, is_unknown, "
                "last_seen, mention_count, importance, status) "
                "VALUES (?, ?, ?, ?, ?, 0, 0.5, 'active')",
                (pid, now, note, 1 if is_unknown else 0, now),
            )
            await db.commit()
        return pid


    async def touch_person(self, person_id: str, delta_mention: int = 1) -> None:
        """见到这个人就把活性刷新一下。

        人本身就是一个原子：有 last_seen、有被提到的次数、有重要性。
        没有关系的人也一样活着——关系只是原子之间的边，不是存在的条件。
        """
        if not person_id:
            return
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                "UPDATE people SET last_seen = ?, "
                "mention_count = COALESCE(mention_count, 0) + ?, "
                "importance = MIN(1.0, 0.25 + 0.1 * LOG(2 + COALESCE(mention_count, 0) + ?)), "
                "status = 'active' WHERE person_id = ?",
                (now, delta_mention, delta_mention, person_id),
            )
            await db.commit()

    async def decay_people(self, quiet_days: float = 7, archive_days: float = 90) -> dict[str, int]:
        """人原子也会冷下去，但不删。

        - 超过 quiet_days 没出现 → status=quiet（还在库里，只是不活跃）
        - 超过 archive_days 没出现 → status=archived（更靠后，依然保留）
        """
        now = time.time()
        quiet_cut = now - max(1.0, quiet_days) * 86400
        arch_cut = now - max(1.0, archive_days) * 86400
        out = {"quiet": 0, "archived": 0}
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE people SET status = 'quiet' WHERE status = 'active' "
                "AND COALESCE(last_seen, created_at) < ?",
                (quiet_cut,),
            )
            out["quiet"] = cur.rowcount
            cur = await db.execute(
                "UPDATE people SET status = 'archived' WHERE status = 'quiet' "
                "AND COALESCE(last_seen, created_at) < ?",
                (arch_cut,),
            )
            out["archived"] = cur.rowcount
            await db.commit()
        return out

    async def active_people(self, limit: int = 20) -> list[dict]:
        """最近活跃的人（按 last_seen 排）。"""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT * FROM people WHERE status != 'archived' "
                "ORDER BY COALESCE(last_seen, created_at) DESC LIMIT ?",
                (max(1, limit),),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def lonely_people(self, limit: int = 20) -> list[dict]:
        """在库里但没有任何关系的人——孤立原子，依然保留。"""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT p.* FROM people p WHERE p.status != 'archived' AND NOT EXISTS ("
                "  SELECT 1 FROM relations r WHERE r.to_ts IS NULL "
                "  AND (r.a_id = p.person_id OR r.b_id = p.person_id)"
                ") ORDER BY COALESCE(p.last_seen, p.created_at) DESC LIMIT ?",
                (max(1, limit),),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_unknown(self, key: str) -> str:
        """拿「待认 id」：同样的 key 永远落到同一个临时 id。"""
        pid = _unknown_id(key)
        async with self._connect() as db:
            cur = await db.execute("SELECT person_id FROM people WHERE person_id = ?", (pid,))
            if await cur.fetchone() is None:
                await db.execute(
                    "INSERT INTO people(person_id, created_at, note, is_unknown, "
                    "last_seen, mention_count, importance, status) "
                    "VALUES (?, ?, ?, 1, ?, 0, 0.5, 'active')",
                    (pid, time.time(), f"待认：{key}", time.time()),
                )
                await db.commit()
        return pid

    async def add_tag(
        self,
        person_id: str,
        kind: str,
        value: str,
        source: str = SOURCE_INFERRED,
        confidence: float | None = None,
        note: str = "",
    ) -> bool:
        """挂一个叫法。已存在同样的 (person, kind, value) 就不重复。"""
        val = str(value or "").strip()
        if not person_id or not val:
            return False
        conf = SOURCE_CONF.get(source, 0.5) if confidence is None else float(confidence)
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT tag_id FROM person_tags WHERE person_id = ? AND kind = ? AND value = ?",
                (person_id, kind, val),
            )
            row = await cur.fetchone()
            if row:
                # 已存在：只把来源往强的方向抬（主人 > 别人 > 我推的）
                await db.execute(
                    "UPDATE person_tags SET confidence = MAX(confidence, ?), seen_at = ? "
                    "WHERE tag_id = ?",
                    (conf, time.time(), int(row["tag_id"])),
                )
            else:
                await db.execute(
                    "INSERT INTO person_tags(person_id, kind, value, source, confidence, seen_at, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (person_id, kind, val, source, conf, time.time(), note),
                )
            await db.commit()
        return True

    async def find_by_tag(self, kind: str, value: str) -> list[dict]:
        """按某个叫法找人。可能返回多条（标签碰撞）。"""
        val = str(value or "").strip()
        if not val:
            return []
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT person_id, kind, value, source, confidence FROM person_tags "
                "WHERE kind = ? AND value = ? ORDER BY confidence DESC",
                (kind, val),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def resolve(
        self,
        *,
        qq: str = "",
        name: str = "",
        create_if_missing: bool = True,
        source: str = SOURCE_INFERRED,
    ) -> str:
        """认人：有 QQ 号用号认，没号用昵称认，认不出给个待认 id。

        - QQ 号命中 → 直接返回那个人
        - 昵称命中且只有一个 → 返回；命中多个（重名）→ 先不合并，走待认
        - 都没有 → 建新的人（QQ 号可认）或者挂待认 id
        """
        qq = str(qq or "").strip()
        name = str(name or "").strip()

        # 1. 按 QQ 号认（最可靠）
        if qq:
            hits = await self.find_by_tag("qq", qq)
            if hits:
                await self.touch_person(hits[0]["person_id"])
                return hits[0]["person_id"]
            if create_if_missing:
                pid = await self.create_person(note=f"来自 QQ {qq}")
                await self.add_tag(pid, "qq", qq, source, SOURCE_CONF.get(source))
                await self.touch_person(pid)
                if name:
                    await self.add_tag(pid, "nickname", name, source)
                return pid

        # 2. 按昵称认
        if name:
            hits = await self.find_by_tag("nickname", name)
            if len(hits) == 1:
                await self.touch_person(hits[0]["person_id"])
                return hits[0]["person_id"]
            if len(hits) > 1:
                # 重名：先不合并，记进待判账本
                await self.push_pending("name_collision", {"name": name, "candidates": hits})
                return await self.get_unknown(f"name:{name}")
            # 昵称没命中但有 QQ 号的话上面已经建过了
            if create_if_missing and not qq:
                pid = await self.create_person(note=f"来自昵称 {name}")
                await self.add_tag(pid, "nickname", name, source)
                await self.touch_person(pid)
                return pid

        # 3. 啥都没有：挂待认
        key = qq or name or "anonymous"
        await self.push_pending("unresolved", {"qq": qq, "name": name})
        return await self.get_unknown(key)

    # ---------- 关系 ----------

    async def add_relation(
        self,
        a_id: str,
        b_id: str,
        rel_type: str,
        *,
        direction: str = "mutual",
        source: str = SOURCE_INFERRED,
        confidence: float | None = None,
        from_ts: float | None = None,
        note: str = "",
        updated_by: str = "",
    ) -> int:
        """加一条关系边。同样的 (a,b,type) 还在生效中就更新它，不重复开。"""
        if not a_id or not b_id or a_id == b_id:
            return 0
        if rel_type not in REL_TYPES:
            note = (note + f" [未知类型:{rel_type}]").strip()
        conf = SOURCE_CONF.get(source, 0.5) if confidence is None else float(confidence)
        now = time.time()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT edge_id FROM relations WHERE a_id = ? AND b_id = ? AND rel_type = ? "
                "AND to_ts IS NULL ORDER BY edge_id DESC LIMIT 1",
                (a_id, b_id, rel_type),
            )
            row = await cur.fetchone()
            if row:
                # 只在置信度不降的前提下更新（主人说的不能被我的推测覆盖）
                await db.execute(
                    "UPDATE relations SET confidence = MAX(confidence, ?), "
                    "source = CASE WHEN ? >= confidence THEN ? ELSE source END, "
                    "updated_by = ?, updated_at = ?, note = ? WHERE edge_id = ?",
                    (conf, conf, source, updated_by, now, note, int(row["edge_id"])),
                )
                eid = int(row["edge_id"])
            else:
                cur = await db.execute(
                    "INSERT INTO relations(a_id, b_id, rel_type, direction, from_ts, to_ts, "
                    "confidence, source, updated_by, updated_at, note) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    (a_id, b_id, rel_type, direction, from_ts or now, conf, source,
                     updated_by, now, note),
                )
                eid = int(cur.lastrowid)
            await db.commit()
        return eid

    async def verify_relation(
        self,
        a_id: str,
        b_id: str,
        rel_type: str,
        *,
        speaker: str = "",
        speaker_kind: str = "other",
        quote: str = "",
        weight: float | None = None,
    ) -> dict[str, Any]:
        """记一条证据，按「多向验证」重算这条关系的置信度。

        **单方说了不算定论**：一个人说破天也只有 0.85；
        要独立的两方以上印证才算 confirmed。
        如果同一对人有互相矛盾的关系（既是朋友又是敌人），标 conflicted 并进待判账本。

        speaker_kind: owner（主人）/ party（当事人）/ other（第三方）/ inferred（我推的）
        """
        if not a_id or not b_id or a_id == b_id or rel_type not in REL_TYPES:
            return {"ok": False, "reason": "参数不合法"}
        if speaker_kind not in SPEAKER_WEIGHT:
            speaker_kind = "other"
        w = SPEAKER_WEIGHT[speaker_kind] if weight is None else float(weight)
        w = min(EVIDENCE_WEIGHT_CAP, max(0.0, w))
        now = time.time()

        # 1. 找到（或新建）这条关系边
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT edge_id FROM relations WHERE a_id = ? AND b_id = ? AND rel_type = ? "
                "AND to_ts IS NULL ORDER BY edge_id DESC LIMIT 1",
                (a_id, b_id, rel_type),
            )
            row = await cur.fetchone()
            if row:
                edge_id = int(row["edge_id"])
            else:
                cur = await db.execute(
                    "INSERT INTO relations(a_id, b_id, rel_type, direction, from_ts, to_ts, "
                    "confidence, source, updated_by, updated_at, note) "
                    "VALUES (?, ?, ?, 'mutual', ?, NULL, 0, ?, ?, ?, '')",
                    (a_id, b_id, rel_type, now, speaker_kind, speaker, now),
                )
                edge_id = int(cur.lastrowid)
                await db.commit()

        # 2. 记证据（同一个 speaker 对同一关系只留一条，更新时间和原话）
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT id FROM relation_evidence WHERE edge_id = ? AND speaker = ? AND rel_type = ?",
                (edge_id, speaker, rel_type),
            )
            row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE relation_evidence SET quote = ?, weight = ?, created_at = ? WHERE id = ?",
                    (quote[:200], w, now, int(row["id"])),
                )
            else:
                await db.execute(
                    "INSERT INTO relation_evidence(edge_id, a_id, b_id, rel_type, speaker, "
                    "speaker_kind, quote, weight, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (edge_id, a_id, b_id, rel_type, speaker, speaker_kind, quote[:200], w, now),
                )
            await db.commit()

        # 3. 独立来源数（不同 speaker 各算一个）
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT speaker, weight FROM relation_evidence WHERE edge_id = ?", (edge_id,)
            )
            evs = await cur.fetchall()
            # 4. 矛盾检测：同一对人有别的类型还在生效
            cur = await db.execute(
                "SELECT rel_type FROM relations WHERE ((a_id = ? AND b_id = ?) OR "
                "(a_id = ? AND b_id = ?)) AND to_ts IS NULL AND rel_type != ?",
                (a_id, b_id, b_id, a_id, rel_type),
            )
            conflicts = [str(r["rel_type"]) for r in await cur.fetchall()]

        weights = [float(e["weight"] or 0) for e in evs]
        source_count = len({str(e["speaker"] or "") for e in evs if e["speaker"]})
        status = evidence_status(source_count, bool(conflicts))
        conf = confidence_from_evidence(weights, conflicted=bool(conflicts))

        async with self._connect() as db:
            await db.execute(
                "UPDATE relations SET confidence = ?, source = ?, updated_by = ?, updated_at = ? "
                "WHERE edge_id = ?",
                (conf, speaker_kind, speaker, now, edge_id),
            )
            await db.commit()

        # 矛盾就记进待判账本
        if conflicts:
            await self.push_pending(
                "relation_conflict",
                {
                    "edge_id": edge_id, "a_id": a_id, "b_id": b_id,
                    "new_type": rel_type, "conflicts": conflicts,
                },
            )

        return {
            "ok": True,
            "edge_id": edge_id,
            "confidence": conf,
            "source_count": source_count,
            "status": status,
            "conflicts": conflicts,
        }

    async def evidence_of(self, edge_id: int) -> list[dict]:
        """一条关系目前的全部证据。"""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT speaker, speaker_kind, quote, weight, created_at FROM relation_evidence "
                "WHERE edge_id = ? ORDER BY weight DESC, created_at DESC",
                (edge_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def end_relation(self, edge_id: int, end_ts: float | None = None) -> bool:
        """关系结束：给这条边写个 to_ts，历史保留。"""
        now = time.time()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE relations SET to_ts = ?, updated_at = ? WHERE edge_id = ? AND to_ts IS NULL",
                (end_ts or now, now, edge_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def relations_of(self, person_id: str, active_only: bool = True) -> list[dict]:
        """某个人现在（或曾经）的所有关系。"""
        if not person_id:
            return []
        cond = "AND to_ts IS NULL" if active_only else ""
        async with self._connect() as db:
            cur = await db.execute(
                f"SELECT * FROM relations WHERE (a_id = ? OR b_id = ?) {cond} "
                "ORDER BY confidence DESC, updated_at DESC",
                (person_id, person_id),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def relation_between(self, a_id: str, b_id: str) -> list[dict]:
        if not a_id or not b_id:
            return []
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT * FROM relations WHERE ((a_id = ? AND b_id = ?) OR (a_id = ? AND b_id = ?)) "
                "AND to_ts IS NULL ORDER BY confidence DESC",
                (a_id, b_id, b_id, a_id),
            )
            return [dict(r) for r in await cur.fetchall()]

    # ---------- 待判账本 ----------

    async def push_pending(self, kind: str, payload: dict) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO pending(kind, payload, created_at, resolved) VALUES (?, ?, ?, 0)",
                (kind, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            await db.commit()

    async def pending_count(self) -> int:
        async with self._connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM pending WHERE resolved = 0")
            return int((await cur.fetchone())[0])

    async def take_pending(self, limit: int = 5) -> list[dict]:
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT * FROM pending WHERE resolved = 0 ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def mark_pending_resolved(self, ids: list[int]) -> int:
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        async with self._connect() as db:
            await db.execute(f"UPDATE pending SET resolved = 1 WHERE id IN ({ph})", ids)
            await db.commit()
        return len(ids)

    async def unknown_count(self) -> int:
        async with self._connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM people WHERE is_unknown = 1")
            return int((await cur.fetchone())[0])

    # ---------- 展示 ----------

    async def describe(self, person_id: str) -> dict[str, Any]:
        """给一个人做张名片：所有叫法 + 所有关系。"""
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT kind, value, source, confidence, seen_at FROM person_tags "
                "WHERE person_id = ? ORDER BY confidence DESC, seen_at DESC",
                (person_id,),
            )
            tags = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute(
                "SELECT person_id, note, is_unknown, last_seen, mention_count, "
                "importance, status FROM people WHERE person_id = ?",
                (person_id,),
            )
            row = await cur.fetchone()
        relations = await self.relations_of(person_id, active_only=True)
        return {
            "person_id": person_id,
            "note": (dict(row) if row else {}).get("note", ""),
            "is_unknown": bool((dict(row) if row else {}).get("is_unknown", 0)),
            "last_seen": (dict(row) if row else {}).get("last_seen", 0),
            "mention_count": (dict(row) if row else {}).get("mention_count", 0),
            "importance": (dict(row) if row else {}).get("importance", 0.5),
            "status": (dict(row) if row else {}).get("status", "active"),
            "tags": tags,
            "relations": relations,
        }

    async def stats(self) -> dict[str, int]:
        async with self._connect() as db:
            out: dict[str, int] = {}
            for key, sql in (
                ("people", "SELECT COUNT(*) FROM people"),
                ("unknown", "SELECT COUNT(*) FROM people WHERE is_unknown = 1"),
                ("tags", "SELECT COUNT(*) FROM person_tags"),
                ("relations", "SELECT COUNT(*) FROM relations WHERE to_ts IS NULL"),
                ("pending", "SELECT COUNT(*) FROM pending WHERE resolved = 0"),
                ("active_people", "SELECT COUNT(*) FROM people WHERE status = 'active'"),
                ("lonely", "SELECT COUNT(*) FROM people p WHERE NOT EXISTS ("
                 "SELECT 1 FROM relations r WHERE r.to_ts IS NULL "
                 "AND (r.a_id = p.person_id OR r.b_id = p.person_id))"),
            ):
                cur = await db.execute(sql)
                out[key] = int((await cur.fetchone())[0])
        return out


__all__ = [
    "PeopleStore", "REL_TYPES", "TAG_KINDS",
    "SOURCE_OWNER", "SOURCE_OTHER", "SOURCE_INFERRED", "SOURCE_CONF",
]

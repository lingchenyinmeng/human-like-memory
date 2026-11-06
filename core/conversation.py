"""Human-like Memory · 对话收集。

把每条消息落库，按会话记录「总结到哪了」，供总结器取用。
不依赖 AstrBot 的对话历史，自己存一份，干净可控。
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from astrbot.api import logger


class ConversationStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def initialize(self) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    sender_id TEXT DEFAULT '',
                    sender_name TEXT DEFAULT '',
                    role TEXT DEFAULT 'user',
                    content TEXT DEFAULT '',
                    group_id TEXT DEFAULT '',
                    platform TEXT DEFAULT '',
                    is_bot INTEGER DEFAULT 0,
                    create_time REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY,
                    last_summarized_id INTEGER DEFAULT 0,
                    updated_at REAL DEFAULT 0
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_session "
                "ON messages(session_id, id)"
            )
            await db.commit()

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            db.row_factory = aiosqlite.Row
            yield db
        finally:
            await db.close()

    async def add_message(
        self,
        session_id: str,
        content: str,
        *,
        sender_id: str = "",
        sender_name: str = "",
        role: str = "user",
        group_id: str = "",
        platform: str = "",
        is_bot: bool = False,
        create_time: float | None = None,
        metadata: dict | None = None,
    ) -> int:
        if not str(content or "").strip():
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO messages (
                    session_id, sender_id, sender_name, role, content,
                    group_id, platform, is_bot, create_time, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, sender_id, sender_name, role, content,
                    group_id, platform, 1 if is_bot else 0,
                    float(create_time or time.time()),
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def last_summarized_id(self, session_id: str) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT last_summarized_id FROM session_state WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def unsummarized(self, session_id: str, limit: int = 120) -> list[dict]:
        """取还没总结的消息（按时间正序）。"""
        last = await self.last_summarized_id(session_id)
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM messages WHERE session_id = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (session_id, last, max(1, limit)),
            )
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def count_unsummarized(self, session_id: str) -> int:
        last = await self.last_summarized_id(session_id)
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND id > ?",
                (session_id, last),
            )
            return int((await cursor.fetchone())[0])

    async def mark_summarized(self, session_id: str, until_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO session_state(session_id, last_summarized_id, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "last_summarized_id = MAX(last_summarized_id, ?), updated_at = ?",
                (session_id, until_id, time.time(), until_id, time.time()),
            )
            await db.commit()

    async def recent_messages(self, session_id: str, limit: int = 20) -> list[dict]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, max(1, limit)),
            )
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in reversed(rows)]

    async def cleanup(self, keep_days: float = 30) -> int:
        """消息正文不必永久保留，总结过就可以清。"""
        cutoff = time.time() - keep_days * 86400
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM messages WHERE create_time < ? AND id <= "
                "(SELECT COALESCE(MAX(last_summarized_id), 0) FROM session_state)",
                (cutoff,),
            )
            await db.commit()
            return cursor.rowcount

    @staticmethod
    def _row_to_msg(row) -> dict:
        def g(k, d=None):
            try:
                return row[k]
            except (IndexError, KeyError):
                return d

        return {
            "id": int(g("id", 0) or 0),
            "session_id": str(g("session_id") or ""),
            "sender_id": str(g("sender_id") or ""),
            "sender_name": str(g("sender_name") or ""),
            "role": str(g("role") or "user"),
            "content": str(g("content") or ""),
            "group_id": str(g("group_id") or ""),
            "platform": str(g("platform") or ""),
            "is_bot": bool(g("is_bot") or 0),
            "create_time": float(g("create_time") or 0),
        }


__all__ = ["ConversationStore"]

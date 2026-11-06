"""Human-like Memory · 语义检索（可选）。

用本机 Ollama 的 bge-m3 算向量，跟关键词结果合并排序。
没装 Ollama 或者没建索引时自动退回纯关键词，不报错、不影响使用。

三块：
  EmbeddingClient  调 Ollama 要向量
  VectorIndex      向量存哪、怎么算相似度
  hybrid_rank      关键词分和向量分怎么合

不引入 faiss：几千条以内用 numpy 暴力算余弦就够快（957 条约 2ms）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import aiosqlite

from astrbot.api import logger

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy 正常都在
    np = None  # type: ignore

DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_MODEL = "bge-m3"


# ---------------- 向量客户端 ----------------


class EmbeddingClient:
    """调 Ollama 拿句子向量。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        model: str = DEFAULT_MODEL,
        timeout: float = 20.0,
    ):
        self.base_url = str(base_url or DEFAULT_BASE).rstrip("/")
        self.model = str(model or DEFAULT_MODEL)
        self.timeout = float(timeout)
        self._dim: int = 0
        self._checked_at: float = 0.0
        self._ok: bool = False

    async def available(self, recheck_after: float = 300.0) -> bool:
        """Ollama 在不在、模型有没有。5 分钟内不重复探测。"""
        now = time.time()
        if self._checked_at and now - self._checked_at < recheck_after:
            return self._ok
        self._checked_at = now
        self._ok = False
        try:
            import aiohttp

            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.base_url}/api/tags", timeout=5) as r:
                    if r.status != 200:
                        return False
                    data = await r.json()
            names = [str(m.get("name") or "") for m in (data.get("models") or [])]
            self._ok = any(self.model.split(":")[0] in n for n in names)
            if self._ok:
                logger.info(f"[human_like_mem] 语义检索可用: {self.model}")
        except Exception as e:
            logger.debug(f"[human_like_mem] Ollama 探测失败: {e!r}")
        return self._ok

    @property
    def dim(self) -> int:
        return self._dim or 1024

    async def embed(self, text: str) -> list[float] | None:
        """一句话 → 向量。失败返回 None。"""
        t = str(text or "").strip()
        if not t:
            return None
        try:
            import aiohttp

            body = json.dumps({"model": self.model, "prompt": t}).encode()
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{self.base_url}/api/embeddings",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout,
                ) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
            vec = data.get("embedding")
            if not isinstance(vec, list) or not vec:
                return None
            if not self._dim:
                self._dim = len(vec)
            return [float(x) for x in vec]
        except Exception as e:
            logger.debug(f"[human_like_mem] embed 失败: {e!r}")
            return None

    async def embed_batch(self, texts: list[str], batch_size: int = 32) -> list[list[float] | None]:
        """批量要向量。优先用 /api/embed（快得多），不支持就退回逐条。"""
        items = [str(t or "").strip() for t in texts]
        if not items:
            return []
        try:
            import aiohttp

            out: list[list[float] | None] = []
            async with aiohttp.ClientSession() as s:
                for i in range(0, len(items), batch_size):
                    chunk = items[i : i + batch_size]
                    body = json.dumps({"model": self.model, "input": chunk}).encode()
                    async with s.post(
                        f"{self.base_url}/api/embed",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        timeout=max(self.timeout, 60.0),
                    ) as r:
                        if r.status != 200:
                            raise RuntimeError(f"HTTP {r.status}")
                        data = await r.json()
                    vecs = data.get("embeddings")
                    if not isinstance(vecs, list):
                        raise RuntimeError("返回里没有 embeddings")
                    for v in vecs:
                        if isinstance(v, list) and v:
                            if not self._dim:
                                self._dim = len(v)
                            out.append([float(x) for x in v])
                        else:
                            out.append(None)
            if len(out) == len(items):
                return out
            raise RuntimeError("批量结果数量不一致")
        except Exception as e:
            logger.debug(f"[human_like_mem] 批量 embed 失败，改逐条: {e!r}")
            return [await self.embed(t) for t in items]


# ---------------- 向量索引 ----------------


class VectorIndex:
    """向量存 SQLite，检索时全部读进内存暴力算余弦。

    单独一张表，不动主表结构；删掉这张表就等于关掉语义检索。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ids: Any = None      # np.ndarray[int]
        self._matrix: Any = None   # np.ndarray[float32]  (n, dim)
        self._loaded_at: float = 0.0
        self._dirty: bool = True

    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 10000")
        return db

    async def initialize(self) -> None:
        db = await self._connect()
        try:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS atom_vectors ("
                "atom_id INTEGER PRIMARY KEY, "
                "vector BLOB, "
                "model TEXT DEFAULT '', "
                "created_at REAL DEFAULT 0)"
            )
            await db.commit()
        finally:
            await db.close()

    async def count(self) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM atom_vectors")
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            await db.close()

    async def save_many(self, items: list[tuple[int, list[float]]], model: str = "") -> int:
        """批量写向量。items: [(atom_id, vector)]"""
        if not items or np is None:
            return 0
        now = time.time()
        db = await self._connect()
        try:
            for atom_id, vec in items:
                arr = np.asarray(vec, dtype=np.float32)
                await db.execute(
                    "INSERT OR REPLACE INTO atom_vectors(atom_id, vector, model, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (int(atom_id), arr.tobytes(), model, now),
                )
            await db.commit()
        finally:
            await db.close()
        self._dirty = True
        return len(items)

    async def missing_ids(self, all_atom_ids: list[int], limit: int = 5000) -> list[int]:
        """哪些原子还没有向量。"""
        if not all_atom_ids:
            return []
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT atom_id FROM atom_vectors")
            have = {int(r[0]) for r in await cursor.fetchall()}
        finally:
            await db.close()
        return [aid for aid in all_atom_ids if int(aid) not in have][:limit]

    async def load(self, force: bool = False) -> bool:
        """把向量读进内存。返回是否可用。"""
        if np is None:
            return False
        if not self._dirty and not force and self._matrix is not None:
            return len(self._ids) > 0
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT atom_id, vector FROM atom_vectors")
            rows = await cursor.fetchall()
        finally:
            await db.close()
        if not rows:
            self._ids = np.zeros(0, dtype=np.int64)
            self._matrix = np.zeros((0, 1), dtype=np.float32)
            self._dirty = False
            return False

        ids: list[int] = []
        vecs: list[Any] = []
        dim = 0
        for atom_id, blob in rows:
            if not blob:
                continue
            arr = np.frombuffer(blob, dtype=np.float32)
            if arr.size == 0:
                continue
            if not dim:
                dim = arr.size
            if arr.size != dim:
                continue
            ids.append(int(atom_id))
            vecs.append(arr)
        if not ids:
            self._ids = np.zeros(0, dtype=np.int64)
            self._matrix = np.zeros((0, 1), dtype=np.float32)
            self._dirty = False
            return False

        mat = np.vstack(vecs).astype(np.float32)
        # 归一化，之后点积就是余弦
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._matrix = mat / norms
        self._ids = np.asarray(ids, dtype=np.int64)
        self._dirty = False
        self._loaded_at = time.time()
        logger.info(f"[human_like_mem] 向量索引已加载: {len(ids)} 条 / {self._matrix.shape[1]} 维")
        return True

    def search(self, query_vec: list[float], top_k: int = 30) -> list[tuple[int, float]]:
        """余弦检索，返回 [(atom_id, 相似度)]，相似度 0-1。"""
        if np is None or self._matrix is None or self._ids is None or len(self._ids) == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        if q.size != self._matrix.shape[1]:
            return []
        n = float(np.linalg.norm(q))
        if n == 0:
            return []
        q = q / n
        sims = self._matrix @ q
        k = min(max(1, top_k), sims.size)
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        out: list[tuple[int, float]] = []
        for i in idx:
            out.append((int(self._ids[i]), float(sims[i])))
        return out


# ---------------- 混合排序 ----------------


def hybrid_rank(
    keyword_scores: dict[int, float],
    vector_scores: dict[int, float],
    *,
    keyword_weight: float = 0.55,
    vector_weight: float = 0.45,
    min_similarity: float = 0.35,
) -> list[tuple[int, float]]:
    """把关键词分和向量分合起来排。

    两路都先归一到 0-1，再按权重相加。只在一边出现的，另一边算 0。
    """
    def _norm(scores: dict[int, float]) -> dict[int, float]:
        if not scores:
            return {}
        lo, hi = min(scores.values()), max(scores.values())
        if hi - lo < 1e-6:
            return {k: 0.6 for k in scores}
        return {k: (v - lo) / (hi - lo) for k, v in scores.items()}

    kw = _norm(keyword_scores)
    # 向量分本身就在 0-1，但要过滤掉不够像的
    vec = {k: v for k, v in vector_scores.items() if v >= min_similarity}

    ids = set(kw) | set(vec)
    out: list[tuple[int, float]] = []
    for aid in ids:
        score = keyword_weight * kw.get(aid, 0.0) + vector_weight * vec.get(aid, 0.0)
        out.append((aid, score))
    out.sort(key=lambda kv: kv[1], reverse=True)
    return out


__all__ = ["EmbeddingClient", "VectorIndex", "hybrid_rank"]

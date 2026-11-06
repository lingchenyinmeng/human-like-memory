"""给记忆原子建向量索引（可选，用于语义检索）。

用法：
    python tools/build_embeddings.py              # 增量：只给还没向量的原子补
    python tools/build_embeddings.py --rebuild    # 全量重建
    python tools/build_embeddings.py --limit 100  # 先试 100 条

需要本机跑着 Ollama，并且有 bge-m3 模型（`ollama pull bge-m3`）。
没装也能跑，脚本会直接告诉你。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_ROOT))
sys.path.insert(0, PLUGIN_ROOT)

from astrbot_plugin_human_like_memory.core.semantic import (  # noqa: E402
    DEFAULT_BASE,
    DEFAULT_MODEL,
    EmbeddingClient,
    VectorIndex,
)
from astrbot_plugin_human_like_memory.tools.migrate_from_livingmemory import (  # noqa: E402
    find_data_dir,
)


def default_db() -> str | None:
    data = find_data_dir()
    if not data:
        return None
    return os.path.join(
        data, "plugin_data", "astrbot_plugin_human_like_memory", "memory.db"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description="给记忆原子建向量索引")
    ap.add_argument("--db", default=default_db(), help="memory.db 路径（默认自动找）")
    ap.add_argument("--base", default=DEFAULT_BASE, help="Ollama 地址")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="嵌入模型")
    ap.add_argument("--rebuild", action="store_true", help="清空重建")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少条")
    args = ap.parse_args()

    if not args.db or not os.path.isfile(args.db):
        print(f"[建索引] 找不到记忆库: {args.db}")
        print("         用 --db 指定，或设 ASTRBOT_DATA_DIR 环境变量")
        raise SystemExit(2)

    print(f"[建索引] 记忆库: {args.db}")
    client = EmbeddingClient(args.base, args.model)
    if not await client.available(recheck_after=0):
        print(f"[建索引] Ollama 不可用或没有 {args.model} 模型。")
        print(f"         先启动 Ollama，然后执行：ollama pull {args.model}")
        raise SystemExit(3)

    index = VectorIndex(args.db)
    await index.initialize()

    if args.rebuild:
        con = sqlite3.connect(args.db)
        con.execute("DELETE FROM atom_vectors")
        con.commit()
        con.close()
        print("[建索引] 已清空旧向量")

    # 取出所有原子的 id + content
    con = sqlite3.connect(f"file:{args.db.replace(os.sep, '/')}?mode=ro", uri=True)
    rows = con.execute("SELECT id, content FROM atoms ORDER BY id").fetchall()
    con.close()
    if not rows:
        print("[建索引] 库里还没有记忆原子，先聊几句再来。")
        return

    all_ids = [int(r[0]) for r in rows]
    content_by_id = {int(r[0]): str(r[1] or "") for r in rows}

    todo = await index.missing_ids(all_ids, limit=args.limit or len(all_ids))
    if not todo:
        print(f"[建索引] {len(all_ids)} 条原子都已有向量，不用建。")
        return
    print(f"[建索引] 共 {len(all_ids)} 条原子，需要补 {len(todo)} 条")

    t0 = time.time()
    done = 0
    batch = 64
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        texts = [content_by_id[a] for a in chunk]
        vecs = await client.embed_batch(texts, batch_size=32)
        pairs = [
            (aid, vec)
            for aid, vec in zip(chunk, vecs)
            if vec is not None
        ]
        if pairs:
            await index.save_many(pairs, model=args.model)
            done += len(pairs)
        pct = min(100, int((i + len(chunk)) / len(todo) * 100))
        print(f"  {pct}%  {done}/{len(todo)}  用时 {time.time()-t0:.0f}s", flush=True)

    total = await index.count()
    print(f"[建索引] 完成：本次写入 {done} 条，索引共 {total} 条，用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())

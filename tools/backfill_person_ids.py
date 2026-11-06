"""给现有记忆原子回填 person_id。

用法：
    python tools/backfill_person_ids.py --dry-run
    python tools/backfill_person_ids.py

做什么：扫 memory.db 的 atoms，把 who_id / who_name 解析成人库的 person_id，
写到 atoms.person_ids。这样「从事件反查人」和「从人筛事件」都能走通。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_ROOT))
sys.path.insert(0, PLUGIN_ROOT)

import aiosqlite

from astrbot_plugin_human_like_memory.core.people import PeopleStore  # noqa: E402
from astrbot_plugin_human_like_memory.tools.migrate_from_livingmemory import (  # noqa: E402
    find_data_dir,
)


def default_paths() -> tuple[str | None, str | None]:
    data = find_data_dir()
    if not data:
        return None, None
    base = os.path.join(data, "plugin_data", "astrbot_plugin_human_like_memory")
    return os.path.join(base, "memory.db"), os.path.join(base, "people.db")


async def main() -> None:
    memory_db, people_db = default_paths()
    ap = argparse.ArgumentParser(description="给记忆回填 person_id")
    ap.add_argument("--memory-db", default=memory_db)
    ap.add_argument("--people-db", default=people_db)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.memory_db or not os.path.isfile(args.memory_db):
        print(f"[回填] 找不到记忆库: {args.memory_db}")
        raise SystemExit(2)
    if not args.people_db:
        print("[回填] 找不到 people.db")
        raise SystemExit(2)

    store = PeopleStore(args.people_db)
    await store.initialize()

    db = await aiosqlite.connect(args.memory_db)
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute(
            "SELECT id, content, who_id, who_name, person_ids FROM atoms ORDER BY id"
        )
        rows = await cur.fetchall()
        print(f"[回填] 共 {len(rows)} 条原子")

        cache: dict[tuple[str, str], str] = {}
        changed = 0
        skipped = 0
        t0 = time.time()

        for r in rows:
            qq = str(r["who_id"] or "").strip()
            name = str(r["who_name"] or "").strip()
            if not qq and not name:
                skipped += 1
                continue

            key = (qq, name)
            if key in cache:
                pid = cache[key]
            else:
                if args.dry_run:
                    pid = "p_dry"
                else:
                    pid = await store.resolve(
                        qq=qq, name=name, create_if_missing=True, source="inferred"
                    )
                cache[key] = pid

            if not pid:
                skipped += 1
                continue
            if args.dry_run:
                changed += 1
                continue

            existing = []
            try:
                data = json.loads(r["person_ids"] or "[]")
                if isinstance(data, list):
                    existing = [str(x) for x in data]
            except Exception:
                existing = []
            if pid in existing:
                continue
            existing.append(pid)
            await db.execute(
                "UPDATE atoms SET person_ids = ? WHERE id = ?",
                (json.dumps(existing, ensure_ascii=False), int(r["id"])),
            )
            changed += 1
            if changed % 200 == 0:
                await db.commit()
                print(f"  {changed}/{len(rows)}  用时 {time.time()-t0:.0f}s", flush=True)
        await db.commit()
    finally:
        await db.close()

    stats = await store.stats()
    print(f"[回填] {'将处理' if args.dry_run else '已处理'} {changed} 条，跳过 {skipped} 条")
    print(f"[回填] 人库: {stats}")


asyncio.run(main())

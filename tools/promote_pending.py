"""按新的转正规则，把卡在暂存区的记忆重新判一遍。

用法：
    python tools/promote_pending.py --dry-run
    python tools/promote_pending.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_ROOT))
sys.path.insert(0, PLUGIN_ROOT)

import aiosqlite

from astrbot_plugin_human_like_memory.core.store import MemoryStore  # noqa: E402
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
    ap = argparse.ArgumentParser(description="重判暂存区的记忆")
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.db or not os.path.isfile(args.db):
        print(f"[转正] 找不到记忆库: {args.db}")
        raise SystemExit(2)

    store = MemoryStore(args.db, {})
    db = await aiosqlite.connect(args.db)
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute(
            "SELECT id, content, mtype, nature, nature_conf, importance, "
            "reinforce_count FROM atoms WHERE status = 'candidate'"
        )
        rows = await cur.fetchall()
        print(f"[转正] 暂存区 {len(rows)} 条，按新规则重判")

        promoted = 0
        reasons: dict[str, int] = {}
        for r in rows:
            ok = store.should_promote(
                mtype=str(r["mtype"] or ""),
                nature_conf=float(r["nature_conf"] or 0),
                importance=float(r["importance"] or 0),
                reinforce_count=int(r["reinforce_count"] or 0),
                nature=str(r["nature"] or ""),
            )
            if not ok:
                continue
            # 记录是哪条规则放行的
            conf = float(r["nature_conf"] or 0)
            imp = float(r["importance"] or 0)
            if int(r["reinforce_count"] or 0) >= store.promote_min_reinforce:
                why = "重复提到"
            elif conf >= store.promote_min_conf:
                why = "定性确定"
            elif str(r["mtype"] or "") in ("fact", "promise") and conf >= store.promote_fact_conf:
                why = "陈述/承诺类"
            else:
                why = "重要性够"
            reasons[why] = reasons.get(why, 0) + 1
            promoted += 1
            if args.dry_run:
                continue
            ttl = 7.0
            await db.execute(
                "UPDATE atoms SET status = 'active', ttl_days = ?, expires_at = ? WHERE id = ?",
                (ttl, time.time() + ttl * 86400, int(r["id"])),
            )
        if not args.dry_run:
            await db.commit()
    finally:
        await db.close()

    print(f"[转正] {'将转正' if args.dry_run else '已转正'} {promoted} 条")
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"   {k}: {v}")


asyncio.run(main())

"""回填定性：给没有 nature 的老记忆补上性质判断。

背景：从 LivingMemory 迁过来的 936 条记忆没有 nature 字段（那个字段是后来才加的），
所以注入时全都被当成「没定性」。这个脚本用当前规则补一遍。

用法：
    python tools/backfill_nature.py --dry-run    # 先看会改多少
    python tools/backfill_nature.py              # 真改（先自动备份）
    python tools/backfill_nature.py --redo-guess # 连之前误标的 joke_guess 一起重判
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_ROOT))
sys.path.insert(0, PLUGIN_ROOT)

from astrbot_plugin_human_like_memory.core.nature import (  # noqa: E402
    classify_nature,
    to_tier,
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
    ap = argparse.ArgumentParser(description="回填记忆的性质判断")
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--redo-guess", action="store_true", help="连 joke_guess 一起重判")
    args = ap.parse_args()

    if not args.db or not os.path.isfile(args.db):
        print(f"[回填] 找不到记忆库: {args.db}")
        raise SystemExit(2)

    import aiosqlite

    if not args.dry_run:
        bak = args.db + f".bak-nature-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(args.db, bak)
        print(f"[回填] 已备份: {bak}")

    cond = "COALESCE(nature,'') = ''"
    if args.redo_guess:
        cond = f"({cond} OR nature = 'joke_guess')"

    db = await aiosqlite.connect(args.db)
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute(f"SELECT id, content, nature FROM atoms WHERE {cond}")
        rows = await cur.fetchall()
        print(f"[回填] 需要处理 {len(rows)} 条")

        changed = 0
        stats: dict[str, int] = {}
        for r in rows:
            nature_raw, conf = classify_nature(str(r["content"] or ""))
            final, src, _ask = to_tier(nature_raw, conf)
            key = final or "(待问)"
            stats[key] = stats.get(key, 0) + 1
            if args.dry_run:
                changed += 1
                continue
            await db.execute(
                "UPDATE atoms SET nature = ?, nature_conf = ?, nature_source = ? WHERE id = ?",
                (final, conf, src or "backfill", int(r["id"])),
            )
            changed += 1
            if changed % 200 == 0:
                await db.commit()
                print(f"  {changed}/{len(rows)}", flush=True)
        await db.commit()
    finally:
        await db.close()

    print(f"[回填] {'将处理' if args.dry_run else '已处理'} {changed} 条")
    print("[回填] 定性分布：")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"   {k}: {v}")


asyncio.run(main())

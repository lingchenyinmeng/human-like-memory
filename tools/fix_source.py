"""修 source 为空的 931 条：补 who_id / source，重算 layer。

规则（按可信度从高到低）：
1. who_name 含主人名 → 补 who_id = 主人号，source 标 private_source_unknown
2. who_name 是别人 → source = 'group'（别人说的话大概率在群里）
3. who_name 也空 → source = 'unknown'，这种不进私聊上下文

**不碰 nature / content / FTS / 向量**。

用法：
    python tools/fix_source.py --dry-run
    python tools/fix_source.py
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

from astrbot_plugin_human_like_memory.core.classifier import classify_layer  # noqa: E402
from astrbot_plugin_human_like_memory.tools.migrate_from_livingmemory import (  # noqa: E402
    find_data_dir,
)

OWNER_ID = "3022774897"
OWNER_NAME = "噩壤"


def default_db() -> str | None:
    data = find_data_dir()
    if not data:
        return None
    return os.path.join(
        data, "plugin_data", "astrbot_plugin_human_like_memory", "memory.db"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description="修 source 为空的记忆")
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--owner-id", default=OWNER_ID)
    ap.add_argument("--owner-name", default=OWNER_NAME)
    args = ap.parse_args()

    if not args.db or not os.path.isfile(args.db):
        print(f"[修source] 找不到记忆库: {args.db}")
        raise SystemExit(2)

    owner_id = str(args.owner_id or "").strip()
    owner_name = str(args.owner_name or "").strip()

    db = await aiosqlite.connect(args.db)
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute(
            "SELECT id, content, who_id, who_name, source, layer, with_owner "
            "FROM atoms WHERE COALESCE(source,'') = '' ORDER BY id"
        )
        rows = await cur.fetchall()
        print(f"[修source] 待修 {len(rows)} 条（source 为空）")

        stats = {"owner": 0, "other_group": 0, "unknown": 0}
        layer_change = 0
        t0 = time.time()

        for r in rows:
            aid = int(r["id"])
            content = str(r["content"] or "")
            who_id = str(r["who_id"] or "").strip()
            who_name = str(r["who_name"] or "").strip()
            old_layer = str(r["layer"] or "")
            old_with_owner = int(r["with_owner"] or 0)

            # 1. 判断这条是谁说的
            is_owner_said = bool(owner_name and who_name and owner_name in who_name)
            if is_owner_said:
                new_who_id = owner_id
                new_source = "private"
                with_owner = 1
                stats["owner"] += 1
            elif who_name:
                new_who_id = who_id
                new_source = "group"
                with_owner = 0
                stats["other_group"] += 1
            else:
                new_who_id = who_id
                new_source = "unknown"
                with_owner = 0
                stats["unknown"] += 1

            # 2. 重算 layer
            new_layer = classify_layer(
                content,
                owner_id=owner_id,
                owner_name=owner_name,
                who_id=new_who_id,
                who_name=who_name,
                source=new_source if new_source != "unknown" else "",
            )
            if new_layer != old_layer:
                layer_change += 1

            if args.dry_run:
                continue
            await db.execute(
                "UPDATE atoms SET who_id = ?, source = ?, layer = ?, with_owner = ? "
                "WHERE id = ?",
                (new_who_id, new_source, new_layer, with_owner, aid),
            )
            if (stats["owner"] + stats["other_group"] + stats["unknown"]) % 300 == 0:
                await db.commit()
                print(f"  {aid}… 用时 {time.time()-t0:.0f}s", flush=True)

        if not args.dry_run:
            await db.commit()
    finally:
        await db.close()

    print(f"[修source] {'将改' if args.dry_run else '已改'}：")
    print(f"  主人说的（补 who_id + source=private）: {stats['owner']}")
    print(f"  别人说的（source=group）: {stats['other_group']}")
    print(f"  来源不明（source=unknown）: {stats['unknown']}")
    print(f"  layer 有变化: {layer_change}")


asyncio.run(main())

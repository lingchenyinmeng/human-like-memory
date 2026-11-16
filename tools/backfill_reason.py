"""给老记忆补判据（nature_reason / nature_evidence / nature_by）。

只补**空着**的，不动已经有的，也不动 nature 本身——
老记忆的性质可能是主人纠正过的，不能拿规则重算覆盖掉。

用法：
    python tools/backfill_reason.py --dry-run
    python tools/backfill_reason.py
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

from astrbot_plugin_human_like_memory.core.nature import resolve_nature  # noqa: E402
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
    ap = argparse.ArgumentParser(description="给老记忆补性质判据")
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用）")
    args = ap.parse_args()

    if not args.db or not os.path.isfile(args.db):
        print(f"[补判据] 找不到记忆库: {args.db}")
        raise SystemExit(2)

    db = await aiosqlite.connect(args.db)
    db.row_factory = aiosqlite.Row
    try:
        # 只挑「有性质但没判据」的
        sql = (
            "SELECT id, content, mtype, nature, nature_reason FROM atoms "
            "WHERE COALESCE(nature,'') != '' AND COALESCE(nature_reason,'') = '' "
            "ORDER BY id"
        )
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur = await db.execute(sql)
        rows = await cur.fetchall()
        print(f"[补判据] 需要补的 {len(rows)} 条（有性质、没判据）")

        filled = 0
        with_ev = 0
        reasons: dict[str, int] = {}
        t0 = time.time()

        for r in rows:
            mtype = str(r["mtype"] or "")
            content = str(r["content"] or "")
            real_nature = str(r["nature"] or "")
            if not content:
                continue

            _n, _c, reason, evidence = resolve_nature(mtype, content)
            # 库里已经有的性质可能被主人改过，判据要跟它对齐
            if _n != real_nature:
                reason = (
                    f"（按 {mtype} 的规则算出来是「{_n}」，"
                    f"库里定的是「{real_nature}」，可能是历史修正）"
                )
                evidence = ""

            reasons[real_nature] = reasons.get(real_nature, 0) + 1
            if evidence:
                with_ev += 1
            filled += 1

            if args.dry_run:
                continue
            await db.execute(
                "UPDATE atoms SET nature_reason = ?, nature_evidence = ?, "
                "nature_by = ? WHERE id = ?",
                (reason[:200], evidence[:200], "rule", int(r["id"])),
            )
            if filled % 400 == 0:
                await db.commit()
                print(f"  {filled}/{len(rows)}  用时 {time.time()-t0:.0f}s", flush=True)

        if not args.dry_run:
            await db.commit()
    finally:
        await db.close()

    print(f"[补判据] {'将补' if args.dry_run else '已补'} {filled} 条，其中 {with_ev} 条带原文证据")
    print("[补判据] 按性质分布：")
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:10]:
        print(f"   {k}: {v}")


asyncio.run(main())

"""重跑分类与定性，并尽量从正文里补出说话人。

用法：
    python tools/reclassify.py --dry-run
    python tools/reclassify.py

解决三件事：
1. mtype 与 nature 打架（先用 mtype 定基调，正则只细化语气）
2. 分类器缺词（"要求"不算命令、"困"不算情绪）→ 已补，这里重跑一遍
3. who_name 大量为空 → 从正文里正则提取「某某说/让/夸…」，能对上人库的补进 person_ids
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_ROOT))
sys.path.insert(0, PLUGIN_ROOT)

import aiosqlite

from astrbot_plugin_human_like_memory.core.classifier import classify_type
from astrbot_plugin_human_like_memory.core.nature import resolve_nature, to_tier
from astrbot_plugin_human_like_memory.core.people import PeopleStore
from astrbot_plugin_human_like_memory.tools.migrate_from_livingmemory import (  # noqa: E402
    find_data_dir,
)

# 正文里「某某说 / 让 / 夸 …」的开头
SPEAKER_RE = re.compile(
    r"^[「『]?([^\s，。、：；!?！？]{2,10})[」』]?\s*"
    r"(?:说|让|叫|问|夸|骂|嫌|要|跟|希望|要求|提醒|答应|告诉|吐槽|抱怨)"
)
# 「某某」在引号里
QUOTED_RE = re.compile(r"^[「『]([^」』]{2,10})[」』]")


def default_paths():
    data = find_data_dir()
    if not data:
        return None, None
    base = os.path.join(data, "plugin_data", "astrbot_plugin_human_like_memory")
    return os.path.join(base, "memory.db"), os.path.join(base, "people.db")


def guess_speaker(text: str, known_names: set[str] | None = None) -> str:
    """从正文开头认说话人。

    不要求「名字+动词」——真实文本经常是「噩壤的昵称是…」「噩壤在2026…」，
    动词表永远补不全。改成：拿开头几个字去人库里比，比得上才算。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.match(r"^[「『]?([^\s，。、：；!?！？]{2,12})", s)
    if not m:
        return ""
    head = m.group(1)
    if known_names:
        # 从长到短试，优先匹配更长的名字
        for n in range(min(len(head), 12), 1, -1):
            cand = head[:n]
            if cand in known_names:
                return cand
        return ""
    # 没给人库时退回老办法（名字+动词）
    for pat in (SPEAKER_RE, QUOTED_RE):
        mm = pat.match(s)
        if mm:
            name = mm.group(1).strip()
            if name and not re.match(r"^(我|他|她|它|这|那|今天|昨天|明天|现在)", name):
                return name
    return ""


async def main() -> None:
    memory_db, people_db = default_paths()
    ap = argparse.ArgumentParser(description="重跑分类与定性")
    ap.add_argument("--memory-db", default=memory_db)
    ap.add_argument("--people-db", default=people_db)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.memory_db or not os.path.isfile(args.memory_db):
        print(f"[重跑] 找不到记忆库: {args.memory_db}")
        raise SystemExit(2)

    people = PeopleStore(args.people_db) if args.people_db else None
    if people:
        await people.initialize()

    db = await aiosqlite.connect(args.memory_db)
    db.row_factory = aiosqlite.Row
    try:
        # 从人库收一份已知名字，用来认说话人
        known: set[str] = set()
        if people:
            try:
                async with people._connect() as pdb:
                    c2 = await pdb.execute(
                        "SELECT value FROM person_tags WHERE kind IN ('nickname','real_name','alias')"
                    )
                    for t in await c2.fetchall():
                        v = str(t[0] or "").strip()
                        if len(v) >= 2:
                            known.add(v)
            except Exception as e:
                print(f"[重跑] 取人名失败（继续）: {e!r}")
        print(f"[重跑] 人库已知名字 {len(known)} 个")

        cur = await db.execute("SELECT * FROM atoms ORDER BY id")
        rows = await cur.fetchall()
        print(f"[重跑] 共 {len(rows)} 条原子")

        fixed_mtype = 0
        fixed_nature = 0
        filled_who = 0
        filled_person = 0
        t0 = time.time()

        for i, r in enumerate(rows, 1):
            content = str(r["content"] or "")
            old_mtype = str(r["mtype"] or "")
            old_nature = str(r["nature"] or "")

            mtype, tconf = classify_type(content)
            nature_raw, nconf = resolve_nature(mtype, content)
            nature_final, nsrc, _ask = to_tier(nature_raw, nconf)

            # 说话人：空着才补
            who_name = str(r["who_name"] or "").strip()
            who_id = str(r["who_id"] or "").strip()
            person_ids = []
            try:
                import json as _json

                data = _json.loads(r["person_ids"] or "[]")
                if isinstance(data, list):
                    person_ids = [str(x) for x in data]
            except Exception:
                person_ids = []

            new_name = who_name
            if not who_name and not who_id:
                guess = guess_speaker(content, known)
                if guess:
                    new_name = guess
            if new_name and new_name != who_name:
                filled_who += 1

            # 能对上人库就补 person_ids
            if people and new_name and not person_ids:
                try:
                    pid = await people.resolve(
                        qq=who_id, name=new_name, create_if_missing=False
                    )
                    if pid and not str(pid).startswith("p_unk_"):
                        person_ids = [pid]
                        filled_person += 1
                except Exception:
                    pass

            changed = (mtype != old_mtype) or (nature_final != old_nature) or (new_name != who_name)
            if changed:
                fixed_mtype += 1 if mtype != old_mtype else 0
                fixed_nature += 1 if nature_final != old_nature else 0

            if args.dry_run or not changed:
                continue

            await db.execute(
                "UPDATE atoms SET mtype = ?, subtype = ?, classify_conf = ?, "
                "nature = ?, nature_conf = ?, nature_source = ?, "
                "who_name = ?, person_ids = ? WHERE id = ?",
                (
                    mtype,
                    str(r["subtype"] or ""),
                    tconf,
                    nature_final,
                    nconf,
                    nsrc or "reclassify",
                    new_name,
                    __import__("json").dumps(person_ids, ensure_ascii=False),
                    int(r["id"]),
                ),
            )
            if i % 200 == 0:
                await db.commit()
                print(f"  {i}/{len(rows)} 用时 {time.time()-t0:.0f}s", flush=True)

        if not args.dry_run:
            await db.commit()
    finally:
        await db.close()

    print(f"[重跑] {'将改' if args.dry_run else '已改'}：")
    print(f"   mtype 变化 {fixed_mtype} 条")
    print(f"   nature 变化 {fixed_nature} 条")
    print(f"   补上说话人 {filled_who} 条")
    print(f"   补上 person_id {filled_person} 条")


asyncio.run(main())

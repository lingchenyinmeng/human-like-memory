"""从现有记忆里抽人，建人际关系库。

用法：
    python tools/build_people.py --dry-run   # 先看能抽出多少人
    python tools/build_people.py             # 真写

做什么：
1. 扫 memory.db 的 atoms（who_id / who_name）
2. 扫 memories 的 participant_identities（sender_id / display_name / aliases）
3. 按「QQ 优先、昵称其次」认人，认不出的挂待认 id
4. 同一个人的多个叫法都挂上标签，标清来源

幂等：重复跑不会重复建人（按 qq / 昵称复用）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_ROOT))
sys.path.insert(0, PLUGIN_ROOT)

from astrbot_plugin_human_like_memory.core.people import (  # noqa: E402
    SOURCE_INFERRED,
    PeopleStore,
)
from astrbot_plugin_human_like_memory.tools.migrate_from_livingmemory import (  # noqa: E402
    find_data_dir,
)


def default_paths() -> tuple[str | None, str | None]:
    data = find_data_dir()
    if not data:
        return None, None
    base = os.path.join(data, "plugin_data", "astrbot_plugin_human_like_memory")
    return os.path.join(base, "memory.db"), os.path.join(base, "people.db")


def _json(v, default):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def collect_people(memory_db: str) -> dict[str, dict]:
    """从记忆库里收集「见过的人」：{key: {qq, names:set}}"""
    out: dict[str, dict] = {}
    con = sqlite3.connect(f"file:{memory_db.replace(os.sep, '/')}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # 1. 原子上的说话人
        for r in con.execute(
            "SELECT who_id, who_name, COUNT(*) AS n FROM atoms "
            "WHERE COALESCE(who_id,'') != '' OR COALESCE(who_name,'') != '' "
            "GROUP BY who_id, who_name"
        ):
            qq = str(r["who_id"] or "").strip()
            name = str(r["who_name"] or "").strip()
            key = qq or f"name:{name}"
            if not key:
                continue
            item = out.setdefault(key, {"qq": qq, "names": set(), "count": 0})
            if qq:
                item["qq"] = qq
            if name:
                item["names"].add(name)
            item["count"] += int(r["n"] or 0)

        # 2. 记忆里的参与者身份（更全，带别名）
        for r in con.execute("SELECT participant_identities FROM memories"):
            idents = _json(r["participant_identities"], [])
            if not isinstance(idents, list):
                continue
            for ident in idents:
                if not isinstance(ident, dict):
                    continue
                qq = str(ident.get("sender_id") or "").strip()
                dname = str(ident.get("display_name") or "").strip()
                key = qq or f"name:{dname}"
                if not key:
                    continue
                item = out.setdefault(key, {"qq": qq, "names": set(), "count": 0})
                if qq:
                    item["qq"] = qq
                if dname:
                    item["names"].add(dname)
                for al in ident.get("aliases") or []:
                    a = str(al or "").strip()
                    if a:
                        item["names"].add(a)
    finally:
        con.close()
    return out


async def main() -> None:
    memory_db, people_db = default_paths()
    ap = argparse.ArgumentParser(description="从记忆里抽人，建人际关系库")
    ap.add_argument("--memory-db", default=memory_db)
    ap.add_argument("--people-db", default=people_db)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.memory_db or not os.path.isfile(args.memory_db):
        print(f"[抽人] 找不到记忆库: {args.memory_db}")
        raise SystemExit(2)
    if not args.people_db:
        print("[抽人] 找不到 people.db 的默认路径")
        raise SystemExit(2)

    people = collect_people(args.memory_db)
    print(f"[抽人] 从记忆里发现 {len(people)} 个不同的身份")

    if args.dry_run:
        rows = sorted(people.values(), key=lambda x: -x["count"])
        for item in rows[:20]:
            names = "/".join(sorted(item["names"])[:3]) or "(无名)"
            print(f"   {item['qq'] or '(无QQ)':<14} {names:<24} 出现 {item['count']} 次")
        print(f"   …… 共 {len(rows)} 条")
        return

    store = PeopleStore(args.people_db)
    await store.initialize()

    created = 0
    tagged = 0
    for key, item in people.items():
        qq = item["qq"]
        names = sorted(item["names"])
        main_name = names[0] if names else ""
        pid = await store.resolve(
            qq=qq, name=main_name, create_if_missing=True, source=SOURCE_INFERRED
        )
        if pid:
            created += 1
        for nm in names:
            if await store.add_tag(pid, "nickname", nm, SOURCE_INFERRED):
                tagged += 1

    stats = await store.stats()
    print(f"[抽人] 处理 {created} 个身份，挂 {tagged} 个标签")
    print(f"[抽人] 人库现状: {stats}")


asyncio.run(main())

"""把 LivingMemory 的记忆迁移到「Human-like Memory」。

用法：
    python tools/migrate_from_livingmemory.py --dry-run          # 先看会迁多少，不写
    python tools/migrate_from_livingmemory.py                   # 真写
    python tools/migrate_from_livingmemory.py --old <旧库> --new <新库>

默认路径按 AstrBot 的插件数据目录猜；猜不到就用参数指定。

迁移规则：
- documents → memories（内容、摘要、key_facts、参与者、重要性、时间原样搬）
- memory_atoms → atoms（能保留的字段全保留：谁说的、定性、隐私、分区）
- 旧原子缺的分类字段（layer/mtype/subtype）用新插件的规则重算
- 状态保持原样（active 还是 active，candidate 还是 candidate）
- 幂等：每条迁过来的记忆在 metadata 里记 source_memory_id，重复跑不会重复插入
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from typing import Any

# 允许从插件根目录导入
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN_ROOT))
sys.path.insert(0, PLUGIN_ROOT)

from astrbot_plugin_human_like_memory.core.conversation import ConversationStore  # noqa: E402
from astrbot_plugin_human_like_memory.core.models import Atom, Memory  # noqa: E402
from astrbot_plugin_human_like_memory.core.store import MemoryStore  # noqa: E402


def _looks_like_data_dir(p: str) -> bool:
    """AstrBot 的 data 目录长这样：下面同时有 plugins/ 和（或）plugin_data/。"""
    return os.path.isdir(os.path.join(p, "plugins")) or os.path.isdir(
        os.path.join(p, "plugin_data")
    )


def find_data_dir() -> str | None:
    """自动找 AstrBot 的 data 目录。

    顺序：
      1. 环境变量 ASTRBOT_DATA_DIR
      2. 从本脚本位置往上找（.../data/plugins/<插件>/tools/本文件）
      3. 从当前工作目录往上找
      4. 常见安装位置
    找不到就返回 None，由调用方提示用户用 --old/--new 指定。
    """
    env = os.environ.get("ASTRBOT_DATA_DIR") or os.environ.get("ASTRBOT_DATA")
    if env and _looks_like_data_dir(env):
        return os.path.abspath(env)

    # 从脚本位置往上
    cur = HERE
    for _ in range(6):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        if _looks_like_data_dir(parent) and os.path.isdir(
            os.path.join(parent, "plugin_data")
        ):
            return parent
        cur = parent

    # 从工作目录往上
    cur = os.getcwd()
    for _ in range(6):
        if _looks_like_data_dir(cur) and os.path.isdir(
            os.path.join(cur, "plugin_data")
        ):
            return cur
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent

    # 常见位置（只是兜底，不保证存在）
    for guess in (
        os.path.expanduser("~/AstrBot/data"),
        r"C:\AstrBot\data",
        "/opt/astrbot/data",
        "/AstrBot/data",
    ):
        if _looks_like_data_dir(guess):
            return os.path.abspath(guess)
    return None


def default_dbs() -> tuple[str | None, str | None]:
    """按探测到的 data 目录拼出旧库和新库的默认路径。"""
    data_dir = find_data_dir()
    if not data_dir:
        return None, None
    old = os.path.join(
        data_dir, "plugin_data", "astrbot_plugin_livingmemory", "livingmemory.db"
    )
    new = os.path.join(
        data_dir, "plugin_data", "astrbot_plugin_human_like_memory", "memory.db"
    )
    return old, new


_DEFAULT_OLD, _DEFAULT_NEW = default_dbs()

# 旧 atom_type → 新 mtype
TYPE_MAP = {
    "factual": "fact",
    "episodic": "fact",
    "preference": "fact",
    "relational": "fact",
    "planned": "promise",
    "unknown": "fact",
}


def _connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True)


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def read_old_memories(old_db: str) -> list[dict]:
    con = _connect_ro(old_db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM documents ORDER BY id").fetchall()
    out: list[dict] = []
    for r in rows:
        meta = _json(r["metadata"], {})
        out.append(
            {
                "old_id": int(r["id"]),
                "text": str(r["text"] or ""),
                "metadata": meta if isinstance(meta, dict) else {},
            }
        )
    con.close()
    return out


def read_old_atoms(old_db: str) -> list[dict]:
    con = _connect_ro(old_db)
    con.row_factory = sqlite3.Row
    cols = {c[1] for c in con.execute("PRAGMA table_info(memory_atoms)")}
    rows = con.execute("SELECT * FROM memory_atoms ORDER BY id").fetchall()
    out: list[dict] = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}

        def g(key, default=None):
            return d.get(key, default) if key in cols else default

        out.append(
            {
                "old_id": int(g("id", 0) or 0),
                "parent": int(g("parent_memory_id", 0) or 0),
                "content": str(g("content", "") or ""),
                "atom_type": str(g("atom_type", "unknown") or "unknown"),
                "importance": float(g("importance", 0.5) or 0.5),
                "confidence": float(g("confidence", 0.6) or 0.6),
                "status": str(g("status", "active") or "active"),
                "create_time": float(g("created_at", time.time()) or time.time()),
                "ttl_days": float(g("ttl_days", 30) or 30),
                "expires_at": float(g("expires_at", 0) or 0),
                "reinforce": int(g("reinforcement_count", 0) or 0),
                # 11.5-11.6 加过的字段（旧库有就搬）
                "who_id": str(g("who_id", "") or ""),
                "who_name": str(g("who_name", "") or ""),
                "said_at": float(g("said_at", 0) or 0),
                "context": _json(g("context"), []),
                "source": str(g("source", "") or ""),
                "with_owner": bool(g("with_owner", 0) or 0),
                "partition_key": str(g("partition_key", "") or ""),
                "topic": str(g("topic", "") or ""),
                "privacy": str(g("privacy", "") or ""),
                "nature": str(g("nature", "") or ""),
                "nature_conf": float(g("nature_conf", 0) or 0),
                "nature_source": str(g("nature_source", "") or ""),
                "session_id": str(g("session_id", "") or ""),
                "persona_id": str(g("persona_id", "") or ""),
            }
        )
    con.close()
    return out


def classify_missing(atom: dict) -> dict:
    """旧原子缺的分类字段用新规则补上。"""
    from astrbot_plugin_human_like_memory.core.classifier import (
        classify_layer,
        classify_subtype,
        classify_type,
    )

    content = atom["content"]
    mtype = TYPE_MAP.get(atom["atom_type"], "fact")
    # 旧库没层信息时按来源猜
    if not atom.get("layer"):
        src = atom.get("source") or ("group" if atom.get("session_id") else "private")
        layer = classify_layer(
            content,
            owner_id="",
            who_id=atom.get("who_id", ""),
            who_name=atom.get("who_name", ""),
            source=src,
            identities=[],
        )
    else:
        layer = atom["layer"]
    # 新类型也重算一次（旧 type 是记忆类型，语义不同）
    mtype2, tconf = classify_type(content)
    subtype = classify_subtype(mtype2, content)
    return {
        "layer": layer,
        "mtype": mtype2 or mtype,
        "subtype": subtype,
        "classify_conf": max(atom.get("confidence") or 0, tconf),
    }


async def migrate(old_db: str, new_db: str, dry_run: bool = False) -> dict:
    if not os.path.isfile(old_db):
        raise SystemExit(f"旧库不存在: {old_db}")

    old_mems = read_old_memories(old_db)
    old_atoms = read_old_atoms(old_db)
    print(f"[迁移] 旧库: {old_db}")
    print(f"[迁移] 找到 {len(old_mems)} 条记忆, {len(old_atoms)} 个原子")

    store = MemoryStore(
        new_db,
        {
            "staging_enabled": True,
            # 迁移不丢数据：旧库里重要性低的原子也要搬过来
            "importance_drop_threshold": 0.0,
            "staging_similarity_threshold": 1.1,  # 禁止查重合并，原样搬
        },
    )
    if not dry_run:
        await store.initialize()

    # 检查是否已经迁过（按 source_memory_id 去重）
    migrated: set[int] = set()
    if not dry_run and os.path.isfile(new_db):
        try:
            con = sqlite3.connect(new_db)
            for (meta,) in con.execute("SELECT metadata FROM memories"):
                d = _json(meta, {})
                if isinstance(d, dict) and d.get("source_memory_id"):
                    migrated.add(int(d["source_memory_id"]))
            con.close()
        except Exception as e:
            print(f"[迁移] 读已迁标记失败（当作没迁过）: {e!r}")
    if migrated:
        print(f"[迁移] 已有 {len(migrated)} 条迁过了，会跳过")

    id_map: dict[int, int] = {}
    mem_ok = mem_skip = 0
    for mem in old_mems:
        if mem["old_id"] in migrated:
            mem_skip += 1
            continue
        meta = mem["metadata"]
        summary = str(
            meta.get("persona_summary") or meta.get("canonical_summary") or mem["text"] or ""
        )
        obj = Memory(
            content=mem["text"] or summary,
            summary=summary,
            personality_summary=summary,
            key_facts=_json(meta.get("key_facts"), []),
            topics=_json(meta.get("topics"), []),
            participants=_json(meta.get("participants"), []),
            participant_identities=_json(meta.get("participant_identities"), []),
            sentiment=str(meta.get("sentiment") or "neutral"),
            importance=float(meta.get("importance", 0.5) or 0.5),
            session_id=str(meta.get("session_id") or ""),
            persona_id=str(meta.get("persona_id") or ""),
            source_session_id=str(meta.get("source_session_id") or ""),
            is_group=bool("GroupMessage" in str(meta.get("source_session_id") or "")),
            create_time=float(meta.get("create_time", mem.get("create_time", time.time())) or time.time()),
            status=str(meta.get("status") or "active"),
            metadata={"source_memory_id": mem["old_id"], "migrated_from": "livingmemory"},
        )
        if dry_run:
            mem_ok += 1
            continue
        try:
            new_id = await store.add_memory(obj)
            id_map[mem["old_id"]] = new_id
            mem_ok += 1
        except Exception as e:
            print(f"[迁移] 记忆 {mem['old_id']} 写入失败: {e!r}")

    # 原子
    atom_ok = atom_skip = 0
    pending_atoms: list[Atom] = []
    for a in old_atoms:
        parent_new = id_map.get(a["parent"], 0)
        if not parent_new and not dry_run:
            atom_skip += 1
            continue
        extra = classify_missing(a)
        obj = Atom(
            memory_id=parent_new,
            content=a["content"],
            layer=extra["layer"],
            mtype=extra["mtype"],
            subtype=extra["subtype"],
            classify_conf=extra["classify_conf"],
            nature=a["nature"],
            nature_conf=a["nature_conf"],
            nature_source=a["nature_source"],
            who_id=a["who_id"],
            who_name=a["who_name"],
            said_at=a["said_at"] or a["create_time"],
            context=a["context"] if isinstance(a["context"], list) else [],
            source=a["source"],
            with_owner=a["with_owner"],
            partition_key=a["partition_key"],
            topic=a["topic"],
            privacy=a["privacy"],
            status=a["status"],
            importance=a["importance"],
            create_time=a["create_time"],
            last_access=a["create_time"],
            reinforce_count=a["reinforce"],
            ttl_days=a["ttl_days"],
            expires_at=a["expires_at"] or (a["create_time"] + a["ttl_days"] * 86400),
            session_id=a["session_id"],
            persona_id=a["persona_id"],
            metadata={"migrated_from": "livingmemory", "source_atom_id": a["old_id"]},
        )
        if dry_run:
            atom_ok += 1
            continue
        pending_atoms.append(obj)

    if not dry_run and pending_atoms:
        # 分批写，避免一次太大
        for i in range(0, len(pending_atoms), 50):
            chunk = pending_atoms[i : i + 50]
            try:
                await store.add_atoms(chunk, enable_dedup=False)
                atom_ok += len(chunk)
            except Exception as e:
                print(f"[迁移] 第 {i // 50 + 1} 批原子写入失败: {e!r}")

    result = {
        "memories_ok": mem_ok,
        "memories_skipped": mem_skip,
        "atoms_ok": atom_ok,
        "atoms_skipped": atom_skip,
        "dry_run": dry_run,
    }
    print(f"[迁移] 结果: {result}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="把 LivingMemory 数据迁到Human-like Memory")
    ap.add_argument(
        "--old", default=_DEFAULT_OLD, help="旧库 livingmemory.db（默认自动探测）"
    )
    ap.add_argument(
        "--new", default=_DEFAULT_NEW, help="新库 memory.db（默认自动探测）"
    )
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = ap.parse_args()

    if not args.old or not args.new:
        print("[迁移] 没能自动找到 AstrBot 的 data 目录。")
        print("       请手动指定：")
        print("         --old <AstrBot/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db>")
        print("         --new <AstrBot/data/plugin_data/astrbot_plugin_human_like_memory/memory.db>")
        print("       或者设环境变量 ASTRBOT_DATA_DIR 指向 AstrBot 的 data 目录。")
        raise SystemExit(2)

    print(f"[迁移] 使用旧库: {args.old}")
    print(f"[迁移] 使用新库: {args.new}")

    if not args.dry_run and os.path.isfile(args.new):
        bak = args.new + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
        import shutil

        shutil.copy2(args.new, bak)
        print(f"[迁移] 已备份新库: {bak}")

    asyncio.run(migrate(args.old, args.new, args.dry_run))


if __name__ == "__main__":
    main()

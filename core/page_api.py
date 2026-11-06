"""面板后端 API。

页面在 views/dashboard/，通过 AstrBot 的 plugin view 机制加载，
调这里的接口拿数据。路由前缀 /astrbot_plugin_human_like_memory/page。

三个接口：
  GET  stats   统计（总数、各状态、各类型、各性质）
  GET  atoms   列表（支持状态/类型/关键词筛选 + 分页）
  GET  atom    单条详情
"""
from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import json_response, request

PLUGIN_NAME = "astrbot_plugin_human_like_memory"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"

STATUS_CN = {
    "candidate": "暂存",
    "active": "正式",
    "dormant": "待定",
    "archived": "遗忘库",
    "expired": "过期",
    "forgotten": "已遗忘",
    "dropped": "已丢弃",
}

TYPE_CN = {
    "command": "命令",
    "promise": "承诺",
    "praise": "夸奖",
    "blame": "批评",
    "fact": "事实",
    "emotion": "情绪",
    "request": "提问",
    "smalltalk": "闲聊",
}

LAYER_CN = {
    "owner_to_me": "主人→她",
    "owner_to_other": "主人→别人",
    "me_to_owner": "她→主人",
    "me_to_other": "她→别人",
    "other_to_me": "别人→她",
    "other_to_other": "别人之间",
}

NATURE_CN = {
    "joke": "玩笑",
    "joke_guess": "多半是玩笑",
    "scold": "真骂",
    "request": "嘱托",
    "question": "提问",
    "emotion": "情绪",
    "fact": "陈述",
}

SUBTYPE_CN = {
    "rule": "规矩",
    "action": "一次性任务",
    "teaching": "教导",
    "person": "人物",
    "event": "事件",
    "observe": "旁观",
    "behavior": "行为批评",
    "identity": "人格否定",
}


def _human_age(ts: float) -> str:
    if not ts:
        return "未知"
    delta = max(0.0, time.time() - float(ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    days = int(delta // 86400)
    if days < 30:
        return f"{days} 天前"
    return f"{days // 30} 个月前"


def _atom_row(atom) -> dict[str, Any]:
    """原子 → 前端要的扁平结构。"""
    return {
        "id": atom.atom_id,
        "content": atom.content,
        "status": atom.status,
        "status_cn": STATUS_CN.get(atom.status, atom.status),
        "layer": atom.layer,
        "layer_cn": LAYER_CN.get(atom.layer, atom.layer),
        "type": atom.mtype,
        "type_cn": TYPE_CN.get(atom.mtype, atom.mtype),
        "subtype": atom.subtype,
        "subtype_cn": SUBTYPE_CN.get(atom.subtype, atom.subtype),
        "nature": atom.nature,
        "nature_cn": NATURE_CN.get(atom.nature, atom.nature),
        "nature_conf": round(float(atom.nature_conf or 0), 2),
        "privacy": atom.privacy,
        "who_id": atom.who_id,
        "who_name": atom.who_name,
        "source": atom.source,
        "with_owner": bool(atom.with_owner),
        "importance": round(float(atom.importance or 0), 2),
        "create_time": atom.create_time,
        "create_text": (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(atom.create_time))
            if atom.create_time
            else "未知"
        ),
        "age": _human_age(atom.create_time),
        "ttl_days": round(float(atom.ttl_days or 0), 1),
        "expires_text": (
            time.strftime("%Y-%m-%d", time.localtime(atom.expires_at))
            if atom.expires_at
            else "-"
        ),
        "partition_key": atom.partition_key,
        "topic": atom.topic,
        "reinforce": int(atom.reinforce_count or 0),
        "memory_id": atom.memory_id,
    }


class PageApi:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        register(f"{PAGE_API_PREFIX}/stats", self.get_stats, ["GET"], "HLM stats")
        register(f"{PAGE_API_PREFIX}/atoms", self.list_atoms, ["GET"], "HLM atoms")
        register(f"{PAGE_API_PREFIX}/atom", self.get_atom, ["GET"], "HLM atom detail")

    # ---------- 接口 ----------

    async def get_stats(self):
        store = getattr(self.plugin, "store", None)
        if store is None:
            return json_response({"status": "error", "message": "插件还没初始化"}, status_code=503)
        try:
            async with store._connect() as db:
                cur = await db.execute("SELECT status, COUNT(*) FROM atoms GROUP BY status")
                by_status = {str(r[0]): int(r[1]) for r in await cur.fetchall()}

                cur = await db.execute("SELECT mtype, COUNT(*) FROM atoms GROUP BY mtype")
                by_type = {str(r[0]): int(r[1]) for r in await cur.fetchall()}

                cur = await db.execute(
                    "SELECT COALESCE(NULLIF(nature,''),'(未定性)'), COUNT(*) FROM atoms GROUP BY 1"
                )
                by_nature = {str(r[0]): int(r[1]) for r in await cur.fetchall()}

                cur = await db.execute("SELECT COUNT(*) FROM memories")
                mem_n = int((await cur.fetchone())[0])

                cur = await db.execute(
                    "SELECT importance FROM atoms WHERE status IN ('active','candidate')"
                )
                imps = [float(r[0] or 0) for r in await cur.fetchall()]
            avg_imp = round(sum(imps) / len(imps), 2) if imps else 0.0
            total = sum(by_status.values())
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "memories": mem_n,
                        "atoms_total": total,
                        "by_status": by_status,
                        "by_status_cn": {
                            STATUS_CN.get(k, k): v for k, v in by_status.items()
                        },
                        "by_type": by_type,
                        "by_type_cn": {TYPE_CN.get(k, k): v for k, v in by_type.items()},
                        "by_nature": by_nature,
                        "avg_importance": avg_imp,
                        "semantic": bool(getattr(store, "vector_index", None)),
                    },
                }
            )
        except Exception as e:
            logger.warning(f"[human_like_mem] 面板统计失败: {e!r}")
            return json_response({"status": "error", "message": str(e)[:200]}, status_code=500)

    async def list_atoms(self):
        store = getattr(self.plugin, "store", None)
        if store is None:
            return json_response({"status": "error", "message": "插件还没初始化"}, status_code=503)
        q = request.query
        status = str(q.get("status") or "").strip()
        mtype = str(q.get("type") or "").strip()
        nature = str(q.get("nature") or "").strip()
        keyword = str(q.get("q") or "").strip()
        try:
            page = max(1, int(q.get("page") or 1))
            size = min(200, max(1, int(q.get("size") or 30)))
        except (TypeError, ValueError):
            page, size = 1, 30

        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if mtype:
            where.append("mtype = ?")
            params.append(mtype)
        if nature:
            where.append("nature = ?")
            params.append(nature)
        if keyword:
            where.append("content LIKE ?")
            params.append(f"%{keyword}%")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        try:
            async with store._connect() as db:
                cur = await db.execute(f"SELECT COUNT(*) FROM atoms {clause}", params)
                total = int((await cur.fetchone())[0])
                cur = await db.execute(
                    f"SELECT * FROM atoms {clause} ORDER BY create_time DESC LIMIT ? OFFSET ?",
                    [*params, size, offset],
                )
                rows = await cur.fetchall()
            atoms = [store._row_to_atom(r) for r in rows]
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "items": [_atom_row(a) for a in atoms],
                        "total": total,
                        "page": page,
                        "size": size,
                        "pages": max(1, (total + size - 1) // size),
                    },
                }
            )
        except Exception as e:
            logger.warning(f"[human_like_mem] 面板列表失败: {e!r}")
            return json_response({"status": "error", "message": str(e)[:200]}, status_code=500)

    async def get_atom(self):
        store = getattr(self.plugin, "store", None)
        if store is None:
            return json_response({"status": "error", "message": "插件还没初始化"}, status_code=503)
        try:
            atom_id = int(request.query.get("id") or 0)
        except (TypeError, ValueError):
            atom_id = 0
        if not atom_id:
            return json_response({"status": "error", "message": "缺少 id"}, status_code=400)
        atom = await store.get_atom(atom_id)
        if atom is None:
            return json_response({"status": "error", "message": "没找到这条记忆"}, status_code=404)
        data = _atom_row(atom)
        data["context"] = atom.context if isinstance(atom.context, list) else []
        data["metadata"] = atom.metadata if isinstance(atom.metadata, dict) else {}
        return json_response({"status": "ok", "data": data})


__all__ = ["PageApi", "PAGE_API_PREFIX"]

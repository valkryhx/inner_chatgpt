"""记忆工具面：``memory_list`` / ``memory_read`` / ``memory_write``。

契约对齐 CC 客户端声明的那三个（``Nji = ["memory_list","memory_read","memory_write"]``，
``m0290:8``，见 ``docs/20261112-Hlaudz记忆系统实施方案.md`` §5）：

- **没有 ``memory_delete``**。CC 客户端没有承诺它，对外承诺 delete 就是超出契约；
  删除靠「覆盖成空」+ 设置页 UI 完成。
- ``memory_list`` **必须分页**（``cursor`` / ``remaining`` / 默认页 50）。
  记忆超过一屏时 cursor 是唯一续读路径。
- ``memory_write`` 带 ``if_version`` 做 CAS：写侧是后台子代理、读侧是主对话，
  用户在设置页还可能手改同一条，没有 CAS 就是后写覆盖前写。

三个工具**只在 root agent 的 registry 里**（§5.2）。子代理拿不到任何 ``memory_*``：
子代理若需要记忆，应由 root 决定给它什么上下文，而不是让它自己翻用户的隐私记忆。
"""
from __future__ import annotations

import json
from typing import Any

from . import memory_store as ms
from .tools import Tool

MEMORY_TOOL_NAMES = frozenset({"memory_list", "memory_read", "memory_write"})


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _err(msg: str) -> str:
    return f"[记忆错误] {msg}"


def build_memory_tools(user_id: str, *, scope: str = ms.SCOPE_PRIVATE) -> list[Tool]:
    """构造绑定到一个用户的记忆工具（闭包捕获 user_id）。

    ``user_id`` 来自 ``AgentContext``，**绝不从工具参数读** —— 参数来自模型，
    模型可以被注入内容骗。这是与路径/查询同级的第三条隔离保证（§2.3）。
    """

    async def memory_list(args: dict) -> str:
        limit = args.get("limit", ms.DEFAULT_PAGE_SIZE)
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = ms.DEFAULT_PAGE_SIZE
        page = await ms.list_memories(
            user_id,
            scope=scope,
            limit=limit_i,
            cursor=(args.get("cursor") or None),
            query=str(args.get("query") or ""),
        )
        payload = page.to_dict()
        if not payload["entries"]:
            return _json({
                "entries": [],
                "remaining": 0,
                "total": 0,
                "note": "该用户还没有任何记忆。",
            })
        return _json(payload)

    async def memory_read(args: dict) -> str:
        slug = str(args.get("slug") or args.get("name") or "").strip()
        if not slug:
            return _err("slug 不能为空（先用 memory_list 看有哪些）")
        spec = await ms.get_memory(user_id, slug, scope=scope)
        if spec is None:
            return _err(f"找不到记忆 {slug}。可用 memory_list 查看现有条目。")
        body = spec.body or ""
        raw = body.encode("utf-8")
        budget = ms.recall_max_bytes()
        truncated = False
        if len(raw) > budget:
            # 单文件召回按**真字节**截（与索引的 code point 口径不同，§3.6）
            body = raw[:budget].decode("utf-8", errors="ignore")
            truncated = True
        return _json({
            "slug": spec.slug,
            "title": spec.title,
            "description": spec.description,
            "kind": spec.kind,
            "tags": list(spec.tags),
            "updated_at": spec.updated_at,
            "version": spec.version,
            "content": body,
            **({"truncated": True,
                "note": f"内容超过 {budget} 字节已截断；可用 file_read 读完整文件 "
                        f"{spec.path.name}（在 memories/{user_id}/{scope}/ 下）"}
               if truncated else {}),
        })

    async def memory_write(args: dict) -> str:
        slug = str(args.get("slug") or args.get("name") or "").strip()
        if not slug:
            return _err("slug 不能为空（小写字母/数字 + '-_.'，如 user-name-pref）")
        body = args.get("content")
        if body is None:
            body = args.get("body")
        if body is None:
            return _err("content 不能为空（这是记忆的正文）")
        tags_raw = args.get("tags")
        tags = None
        if isinstance(tags_raw, list):
            tags = [str(t) for t in tags_raw]
        elif isinstance(tags_raw, str) and tags_raw.strip():
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        res = await ms.write_memory(
            user_id,
            slug=slug,
            title=str(args.get("title") or ""),
            description=str(args.get("description") or ""),
            body=str(body),
            kind=str(args.get("kind") or ""),
            tags=tags,
            scope=scope,
            if_version=(str(args["if_version"]) if args.get("if_version") else None),
        )
        if res.outcome == "conflict":
            return _json({
                "outcome": "conflict",
                "message": res.message,
                "current_version": res.current_version,
                "currentContent": res.current_content,
            })
        if not res.ok or res.spec is None:
            return _json({"outcome": res.outcome, "message": res.message})
        out = {"outcome": "ok", **res.spec.to_public_dict()}
        # 「有没有 description」按**本次调用给了什么**判断，不按最终文件判断：
        # 解析器对空 description 会回落到正文首行（为了兼容手写文件），
        # 那会让这条提醒永远不触发，而模型恰恰最需要它。
        if str(args.get("description") or "").strip():
            out["note"] = "已写入。下一轮会自动注入这条记忆的摘要。"
        else:
            out["note"] = (
                "已写入，但 description 为空 —— 它是未来判断相关性的唯一依据，"
                "强烈建议补一句「这条记的是什么」（带上 if_version 再写一次即可）。"
            )
        return _json(out)

    return [
        Tool(
            "memory_list",
            "列出当前用户的记忆（分页）。记忆是跨会话的长期事实：用户偏好、称呼、"
            "项目状态、外部资源指针。想看有什么记忆、或不确定 slug 时先调它。"
            "默认每页 50 条；返回里的 next_cursor 传给下一次 cursor 可续读，"
            "remaining 是还剩多少条。",
            {
                "type": "object",
                "properties": {
                    "cursor": {"type": "string",
                               "description": "上一次返回里的 next_cursor；省略=从第一条开始"},
                    "limit": {"type": "integer", "description": "本页条数，默认 50，最大 200"},
                    "query": {"type": "string",
                              "description": "可选：按 slug/标题/描述/标签做关键词过滤"},
                },
                "required": [],
            },
            memory_list,
        ),
        Tool(
            "memory_read",
            "读取一条记忆的全文。先用 memory_list 拿到 slug，再用它读正文。",
            {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "记忆的 slug（来自 memory_list）"},
                },
                "required": ["slug"],
            },
            memory_read,
        ),
        Tool(
            "memory_write",
            "写入一条记忆（新建或更新同 slug）。只在「这条信息跨会话仍然有用」时才写："
            "用户的长期偏好、称呼、明确的工作方式纠正、项目长期状态。"
            "不要写本次对话的临时内容、不要写你自己推测的东西、不要重复已有的条目"
            "（先 memory_list 看看）。description 是未来判断相关性的唯一依据，"
            "要具体到「一看就知道这条讲什么」。更新已有条目时带上 if_version 可避免覆盖别人的修改。",
            {
                "type": "object",
                "properties": {
                    "slug": {"type": "string",
                             "description": "标识符：小写字母/数字 + '-_.'，如 user-name、proj-status"},
                    "title": {"type": "string", "description": "短标题（展示用）"},
                    "description": {"type": "string",
                                    "description": "一句话描述：未来判断相关性就用它，要具体"},
                    "content": {"type": "string", "description": "正文（markdown）"},
                    "kind": {"type": "string",
                             "enum": list(ms.KINDS),
                             "description": "user=关于这个人 / feedback=对工作方式的纠正或确认 / "
                                            "project=项目长期状态 / reference=外部资源指针"},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "关键词标签，用于命中加权"},
                    "if_version": {"type": "string",
                                   "description": "可选：更新时带上当前 version（乐观锁，冲突则返回最新内容）"},
                },
                "required": ["slug", "content"],
            },
            memory_write,
        ),
    ]

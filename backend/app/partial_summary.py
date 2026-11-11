"""上游彻底不可用时，给用户一个「已经做完了什么」的收尾。

背景（真机 runId 131321-558a）：三条任务全部 completed、三个 worker 都正常结束，
但 lead 在收尾阶段撞上上游持续 502，用户最后看到的是一句半截话 ——
劳动成果都在任务板和数据库里，却没有任何东西告诉用户。

关键约束：**这段收尾绝不能再调模型**。它存在的场景就是模型服务已经不可用，
任何依赖 LLM 的兜底都会跟着一起失败。所以这里只做数据库读取 + 模板拼装。
"""
from __future__ import annotations

from typing import Any

from . import team_store
from .scope import TeamScope
from .subagent import store as sub_store

# 单条结果/任务在摘要里最多占多少字符。摘要要一眼能看完，不是把结论再抄一遍。
_PREVIEW_CHARS = 240
_MAX_ITEMS = 8


def _flat(text: str, limit: int = _PREVIEW_CHARS) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _status_label(status: str) -> str:
    return {
        "completed": "已完成",
        "in_progress": "进行中",
        "pending": "未开始",
        "cancelled": "已取消",
    }.get(str(status or ""), str(status or "未知"))


async def _latest_finished_generation(
        session_id: str, user_id: str) -> tuple[list[dict], list[dict]] | None:
    """取本会话最近一次有产出的那一代的（任务, 队友运行记录）。

    只用于「本轮什么都没做就挂了」的兜底：告诉用户上一轮的成果还在。
    读库失败一律当作没有，绝不因为兜底逻辑把收尾本身搞挂。
    """
    try:
        recent_runs = await sub_store.list_runs(session_id, user_id)
    except Exception:
        return None
    if not recent_runs:
        return None
    # 从最近往前找第一个「有完成结果或任务板记录」的 generation
    seen: list[str] = []
    for r in reversed(recent_runs):
        gen = str(r.get("generation_id") or "")
        if gen and gen not in seen:
            seen.append(gen)
    for gen in seen:
        gen_runs = [r for r in recent_runs if str(r.get("generation_id") or "") == gen]
        team_id = str(gen_runs[0].get("team_id") or "")
        tasks: list[dict] = []
        if team_id:
            scope = TeamScope(user_id=user_id, session_id=session_id,
                              generation_id=gen, team_id=team_id,
                              agent_id="root", role="lead")
            try:
                tasks = await team_store.list_tasks(scope)
            except Exception:
                tasks = []
        if tasks or any(str(r.get("result_text") or "") for r in gen_runs):
            return tasks, gen_runs
    return None


async def build_partial_summary(*, user_id: str, session_id: str,
                                generation_id: str, team_id: str,
                                user_text: str = "") -> str:
    """拼一段「中断前已经完成了什么」的说明。没有可汇报内容时返回空串。"""
    scope = TeamScope(user_id=user_id, session_id=session_id,
                      generation_id=generation_id, team_id=team_id or "",
                      agent_id="root", role="lead")

    tasks: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    if team_id:
        try:
            tasks = await team_store.list_tasks(scope)
        except Exception:
            tasks = []
    try:
        runs = await sub_store.list_runs(
            session_id, user_id,
            generation_id=generation_id, team_id=team_id or "")
    except Exception:
        runs = []

    # 本轮没有成果时，回退到本会话**最近**结束的那一代。
    # 用户这一句很可能就是在延续上一轮（「再总结一下刚才的结果」），
    # 只盯着当前 generation 会得出「什么都没做」从而什么都不汇报，
    # 而现在恰恰是他最需要知道上一轮成果没丢的时候。
    if not tasks and not runs:
        latest = await _latest_finished_generation(session_id, user_id)
        if latest:
            tasks, runs = latest

    if not tasks and not runs:
        return ""

    done = [t for t in tasks if str(t.get("status")) == "completed"]
    unfinished = [t for t in tasks if str(t.get("status")) != "completed"]
    ok_runs = [r for r in runs
               if str(r.get("status")) == "completed" and (r.get("result_text") or "")]
    bad_runs = [r for r in runs
                if str(r.get("status")) in ("errored", "stopped")]

    lines: list[str] = []
    lines.append("> **执行中断说明**")
    if user_text:
        lines.append(f"> 你的请求「{_flat(user_text, 120)}」在收尾阶段被上游模型服务中断打断。")
    else:
        lines.append("> 这次请求在收尾阶段被上游模型服务中断打断。")
    lines.append("")

    if done or ok_runs:
        lines.append("**中断前已完成**")
        listed = 0
        for t in done[:_MAX_ITEMS]:
            owner = str(t.get("owner_agent_id") or "").removeprefix("sub:") or "未指派"
            lines.append(f"- ✅ {_flat(t.get('subject'), 90)}（{owner}）")
            listed += 1
        # 任务板上没有、但队友确实产出了结论的，也要算进"已完成"
        covered = {str(t.get("owner_agent_id") or "") for t in done}
        for r in ok_runs[:_MAX_ITEMS]:
            if str(r.get("agent_id") or "") in covered:
                continue
            name = str(r.get("name") or r.get("agent_id") or "队友")
            lines.append(f"- ✅ {name}：{_flat(r.get('result_text'))}")
            listed += 1
            if listed >= _MAX_ITEMS:
                break
        lines.append("")

    if unfinished:
        lines.append("**尚未完成**")
        for t in unfinished[:_MAX_ITEMS]:
            lines.append(f"- ⏳ {_flat(t.get('subject'), 90)}"
                         f"（{_status_label(t.get('status'))}）")
        lines.append("")

    if bad_runs:
        names = "、".join(str(r.get("name") or r.get("agent_id") or "队友")
                         for r in bad_runs[:5])
        lines.append(f"**执行失败的队友**：{names}")
        lines.append("")

    lines.append("**接下来可以怎么做**")
    lines.append("- 直接重发一次即可：上面的进度都在任务板和上下文里，不会丢。")
    if unfinished:
        lines.append("- 如果只想补完剩下的部分，可以点名让它继续未完成的任务。")
    return "\n".join(lines).strip()
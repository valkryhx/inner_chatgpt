"""把 AgentLoop 的事件归一化为前端 SSE 事件。

事件协议（对齐 Aivory）：
  message_start / text_delta / thinking_delta / tool_start / tool_input /
  tool_result / chart / compaction / done / error

chart 事件来自工具返回的自描述 artifact（{"type":"chart",...}），
不依赖任何具体工具名。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


def sse_frame(event_type: str, data: dict[str, Any], event_id: int | None = None) -> str:
    lines = [f"event: {event_type}"]
    lines.append("data: " + json.dumps(data, ensure_ascii=False))
    if event_id is not None:
        lines.append(f"id: {event_id}")
    return "\n".join(lines) + "\n\n"


def _tool_output_to_text(output: Any) -> str:
    content = getattr(output, "content", None)
    if content is None:
        return str(output)
    if isinstance(content, str):
        return content
    return str(content)


_ARTIFACT_TYPES = ("chart", "table")


def _json_objects_from_text(text: str) -> list[dict[str, Any]]:
    """从工具返回文本里提取「自描述 artifact」JSON 对象。

    约定（通用、不绑定任何工具名）：MCP 工具若返回形如 {"type":"chart", ...}
    的结果，平台就把它识别成可渲染 artifact。兼容三种常见包装：
      1) 纯 JSON
      2) MCP 文本内容列表 [{"type":"text","text":"<json>"}]
      3) {"content":[{"type":"text","text":"<json>"}]}
    """
    candidates: list[str] = []
    raw = text.strip()
    if not raw:
        return []
    candidates.append(raw)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                candidates.append(item["text"])
    elif isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    candidates.append(item["text"])

    found: list[dict[str, Any]] = []
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("type") in _ARTIFACT_TYPES:
            found.append(obj)
    return found


def detect_artifacts(output_text: str) -> list[dict[str, Any]]:
    """公开入口：把工具输出里的自描述 artifact 抽出来（目前用于图表）。"""
    return _json_objects_from_text(output_text)


def _ensure_steer_injector(ur, user_id: str, session_id: str) -> None:
    """给「没有 team 的普通对话」装用户插话注入器。

    team 路径的注入器已经会 drain 用户队列（见 subagent/session.py），
    这里靠 ``_steer_aware`` 标记跳过，避免同一条消息被注入两次。

    ⚠️ **必须每代重装**（不能靠 ``_steer_aware`` 只装一次）。
    ``ur`` 是 ``RuntimeHub`` 按 ``(user, model)`` 缓存的，同一个 loop 会跨
    会话、跨 generation 复用；而注入器闭包捕获的是**本次** generation 的
    队列键。装一次就锁死的话，第二代起插话全部进不来 ——
    实测探针（2026-11-14）：gen1 收到，gen2 空。

    但「每代重装」与「team 路径不许再装一个」是两件事，要分开表达：
    用 ``_steer_owner`` 记录**谁在 drain 这个队列**。

      - ``"plain"``：本函数装的，每代重装（闭包里的 generation 要更新）；
      - ``"team"``：`subagent/session.py` 装的，本函数**必须让位**，
        否则同一条插话会被两个注入器各取一次。

    这里只写自己的槽位，由 ``memory_inject.rebuild_message_injector``
    按固定顺序（记忆 -> steer -> 原始）重建 —— 各安装器自己记 baseline
    会互相污染（记忆把第一代的 steer 记成 baseline，第二代又拼回去）。
    """
    loop = getattr(ur, "loop", None)
    if loop is None:
        return
    # team 路径（或任何外部安装者）已经在 drain：让位。
    # 兼容只有 ``_steer_aware`` 没有 ``_steer_owner`` 的旧路径（视为外部）。
    owner = getattr(loop, "_steer_owner", "")
    if owner and owner != "plain":
        return
    if not owner and getattr(loop, "_steer_aware", False):
        return
    generation_id = str(getattr(ur, "generation_id", "") or "")
    if not generation_id:
        # 拿不到 generation 就无法定位队列；普通路径宁可插话失效，
        # 也不能把消息塞进一个谁也取不到的队列。
        return

    def _inject() -> list[dict[str, Any]]:
        try:
            from .steer_queue import get_queue, wrap_user_steer
            q = get_queue(user_id, session_id, generation_id)
            return [{"role": "user", "content": wrap_user_steer(item["text"])}
                    for item in q.drain()]
        except Exception:  # noqa: BLE001 - 插话读失败不能拖垮本轮
            return []

    try:
        from .memory_inject import rebuild_message_injector
        loop._steer_injector = _inject
        rebuild_message_injector(loop)
    except Exception:  # noqa: BLE001 - 插话失效不该让整轮起不来
        loop.message_injector = _inject
    loop._steer_owner = "plain"
    loop._steer_aware = True


def _install_memory_injector(ur, user_id: str, session_id: str, user_text: str) -> None:
    """把记忆注入器**包在**现有注入器外面（不覆盖它）。

    三件事都不能省：
      1. 必须在 ``run_stream`` 之前装完 —— ``AgentLoop._injector_guard_now()``
         按注入器对象身份惰性绑定 guard，轮中途换注入器会丢掉在飞的后台 task；
      2. 不能因为 ``_steer_aware`` 为真就跳过 —— 那个标记的语义是
         「已有注入器在 drain 用户插话队列」，不是「不许再装注入器」。
         跳过只会导致「记忆永远不注入」；
      3. 失败要吞掉 —— 记忆是增强，装不上不该让整轮起不来。
    """
    loop = getattr(ur, "loop", None)
    if loop is None:
        return
    # 记忆读侧按档位裁剪（见 tool_policy.allows_memory_read）：
    # chat_only 的契约是「什么都不碰」，连记忆都不该看到。
    # read_only 保留读 —— 读的边界是 user_id，token 已经绑死。
    try:
        from .tool_policy import allows_memory_read
        if not allows_memory_read(getattr(ur, "tool_policy", "") or "full"):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        from .memory_inject import install_memory_injector
        # team 路径（subagent/session.py）已经装过记忆注入器。此时**不能**再装一个：
        # 两个 MemoryInjector 各有自己的 _injected_slugs，同一批记忆会注入两遍。
        # 只把本轮的检索线索补进去即可。
        if getattr(loop, "_memory_aware", False):
            existing = getattr(loop, "_memory_injector", None)
            if existing is not None:
                existing.set_query_hint(user_text)
            return
        install_memory_injector(loop, user_id, query_hint=user_text)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("记忆注入器装配失败，本轮不注入", exc_info=True)


def _install_memory_turn_end(ur, user_id: str) -> None:
    """给本轮的 loop 挂「轮次收尾」钩子（后台记忆提取，M4）。

    为什么挂在 loop 上而不是在 sse 里 yield done 之后调：
    ``run_agent_stream`` 是 SSE 的生产者，它 yield ``done`` 之后整条流就关了，
    之后再也拿不到「本轮到底发生了什么」（history 还在，但没有明确的收尾点）。
    而 ``AgentLoop`` 内部知道「正常完成」这个出口在哪，且它已经持有 history。

    为什么 ``AgentLoop`` 不直接知道记忆：钩子是它唯一知道的形态。
    这样 agent_loop 不 import 记忆模块，记忆关了也不会在核心里留死代码。

    ⚠️ 按 ``tool_policy`` 裁剪：这个钩子会**写记忆**，且不经过 registry，
    所以 ``tool_policy`` 的常规兜底（「registry 里没有该工具」）管不到它。
    只读 / 纯对话档若挂上，就等于给了它们一条绕过 registry 的写通道，
    写入的内容还会在下一个会话里被当成用户事实注入（M5 发现的缺陷）。

    ⚠️ 读侧注入（``_install_memory_injector``）**不裁**：记忆的读边界是
    user_id，token 已经绑死，读自己的记忆不构成越权。
    """
    loop = getattr(ur, "loop", None)
    if loop is None:
        return
    try:
        from .tool_policy import allows_memory_write
        if not allows_memory_write(getattr(ur, "tool_policy", "") or "full"):
            return
    except Exception:  # noqa: BLE001
        pass
    # team 路径（subagent/session.py）已经在构造里挂了钩子。
    # 一个 loop 只能有一个 on_turn_end，重复挂会顶掉前一个。
    if getattr(loop, "_memory_aware", False):
        return
    try:
        from .memory_extract import make_turn_end_hook
        loop.on_turn_end = make_turn_end_hook(ur)
    except Exception:  # noqa: BLE001 - 提取是增强，装不上不该让整轮起不来
        import logging
        logging.getLogger(__name__).warning("记忆提取钩子装配失败，本轮不提取", exc_info=True)


async def _install_memory_prefetch(ur, user_id: str) -> None:
    """给本轮的 loop 装记忆预取（M3.5）。默认关闭时什么都不做。

    为什么在这里装而不是在 `MemoryInjector` 里：
    预取要「先启动、稍后消费」，而注入器是「每轮开头同步返回」的形态 ——
    合进注入器就退化成同步等待，选择器 300-2000ms 会直接进 TTFT。

    选择器 client 是**独立**的小模型（`HLZ_MEMORY_SELECTOR_MODEL`），
    不复用主模型：一个 10 步任务会让主模型多跑 10 次。
    client 每代新建，不缓存 —— 它是纯 HTTP 客户端，构造很便宜，
    而缓存会带来「用户改了配置要等重启」的问题。

    档位闸：预取是记忆的**第二条读路径**（开选择器时关键词注入让位，
    所以它实际上是唯一的那条）。M5 首版只关了关键词注入，``chat_only``
    通过预取照样能拿到记忆 —— 两条读路径必须用同一个判据
    （``allows_memory_read``），否则「关掉一条」只是把门换了个位置。
    """
    loop = getattr(ur, "loop", None)
    if loop is None:
        return
    try:
        from .tool_policy import allows_memory_read
        if not allows_memory_read(getattr(ur, "tool_policy", "") or "full"):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        from .memory_prefetch import install_prefetch, prefetch_enabled
        if not prefetch_enabled():
            return
        from .memory_prefetch import selector_client_for
        client = await selector_client_for(user_id)
        install_prefetch(loop, client, user_id)
    except Exception:  # noqa: BLE001 - 预取是增强，装不上不该让整轮起不来
        import logging
        logging.getLogger(__name__).warning(
            "记忆预取装配失败，本轮走关键词排序", exc_info=True)


async def run_agent_stream(
    ur,
    session_id: str,
    user_text: str,
    stop_event: asyncio.Event | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Load history, append the user message, run the AgentLoop, yield normalized events.

    Event shapes match the frontend protocol:
      text_delta / thinking_delta / tool_start / tool_result / chart / done / error
    """
    store = getattr(ur, "store", None)
    user_id = str(getattr(ur, "user_id", "") or "")
    if store is None:
        # Compatibility with minimal test runtimes; production UserRuntime
        # always has a HistoryStore.
        history = []
    if store is not None:
        if user_id:
            history = await store.get(user_id, session_id)
        else:
            history = await store.get(session_id)
    user_msg: dict[str, Any] = {"role": "user", "content": user_text}
    if attachments:
        # 只存引用（path/mime/name），base64 在送模型时才展开。
        user_msg["attachments"] = attachments
    history.append(user_msg)
    if store is not None:
        if user_id:
            await store.set(user_id, session_id, history)
        else:
            await store.set(session_id, history)

    _ensure_steer_injector(ur, user_id, session_id)
    # 记忆放最后装：rebuild_message_injector 按固定顺序拼装
    # （记忆 -> steer -> 原始），所以装配先后不影响结果顺序，
    # 但要保证两边都写进槽位后 message_injector 是最终形态。
    _install_memory_injector(ur, user_id, session_id, user_text)
    # 轮次收尾钩子（M4 后台提取）。同样必须在 run_stream 之前挂。
    _install_memory_turn_end(ur, user_id)
    # 记忆预取（M3.5）。必须在 run_stream 之前装，且**在**注入器之后 ——
    # 它要在「模型调用前」就被 AgentLoop 看到。
    await _install_memory_prefetch(ur, user_id)

    async for ev in ur.loop.run_stream(history, stop_event=stop_event, session_id=session_id):
        et = ev.get("type")
        if et == "tool_result":
            content = ev.get("content", "") or ""
            yield ev
            for artifact in detect_artifacts(content):
                yield {"type": artifact["type"], "spec": artifact}
        elif et in ("text_delta", "thinking_delta", "tool_start", "done", "error"):
            yield ev
        else:
            yield ev

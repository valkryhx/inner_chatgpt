"""agent 动作时间轴：每一轮「第几毫秒做了什么」落一行。

## 解决什么问题

2026-11-20 三次 agent team 分别跑了 5.4 / 17.9 / 24.4 分钟。事后想回答
一个最简单的问题——**这是「活多所以慢」，还是「卡了很多次」？**——
现有日志答不出来：

| 现有可观测性 | 能回答什么 |
| --- | --- |
| `subagent_runs.turns` | 完成了几轮（中途恒 0，看不出进展） |
| `prelude` 的 `turn_ms` | 整轮多久（一个数，没有中间过程） |
| `team-*.jsonl` | 团队事件（派单/收单，不含 LLM 与工具内部动作） |

缺的是**时间轴**：第 12 秒在搜索、第 45 秒收到结果、第 90 秒开始写正文。
有了它，「慢」和「卡」在日志上长得完全不一样：

    t=12000ms  tool_start web_search_exa   query=特朗普 2026 政策
    t=45000ms  tool_result web_search_exa
    t=47000ms  text               +234 字
    t=900000ms text               +512 字   ← 中间静默 14 分钟 = 可疑

## 三处设计取舍

1. **按时间窗聚合 `text_delta`**：流式 token 每帧都记会写出几十万行，
   日志直接不可用。但也不能丢——丢了就看不出「文字是什么时候到的」。
   所以按 `_FLUSH_MS` 窗口聚合成一条，只记字数。

2. **摘要硬截断 50 字**：tool 参数里可能是整个文件内容，
   不截断单行能到几十 KB，`grep` 就没法用了。

3. **默认开、可关**：这条是排查「慢 vs 卡」的唯一依据，
   关掉等于没有可观测性。`HLZ_ACTIVITY_TRACE=0` 可关（纯性能场景）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("hlaudz.activity")

# text_delta 聚合窗口。太大 ⇒ 时间分辨率差；太小 ⇒ 行数爆炸。
# 1000ms 与 CC 的 `dn = min(stall_ms * 0.1, 1000)` 同级，
# 在这个粒度下「静默 14 分钟」一眼能看出来。
_FLUSH_MS = 1000.0

# 摘要正文上限（字）。
_BRIEF_MAX = 50

# 测试用：把落盘替换成内存收集。生产恒为 None。
_SINK: list[dict[str, Any]] | None = None


def enabled() -> bool:
    raw = (os.environ.get("HLZ_ACTIVITY_TRACE") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def reset_for_test() -> None:
    global _SINK
    _SINK = []


def drain_for_test() -> list[dict[str, Any]]:
    return list(_SINK or [])


def render_line_for_test(row: dict[str, Any], gen: str, label: str) -> str:
    return _render(row, gen, label)


def _render(row: dict[str, Any], gen: str, label: str) -> str:
    return (f"agent-activity gen={gen} who={label} t={int(row['t'])}ms "
            f"action={row['action']} brief={row['brief']}")


def _summarize(payload: Any) -> str:
    """把任意 tool 参数压成一句 50 字以内的摘要。

    优先取最有信息量的键（query / path / code / command），
    都没有时退化成 JSON 片段 —— 总比空着强。
    """
    if payload is None:
        return ""
    if not isinstance(payload, dict):
        return " ".join(str(payload).split())[:_BRIEF_MAX]
    for key in ("query", "q", "keyword", "pattern", "url", "path", "file_path",
                "command", "code", "prompt", "text", "content", "name"):
        v = payload.get(key)
        if not v:
            continue
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        s = " ".join(s.split())
        if s:
            return f"{key}={s[:_BRIEF_MAX]}"
    try:
        s = json.dumps(payload, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - 序列化失败不能影响埋点
        s = str(payload)
    return " ".join(s.split())[:_BRIEF_MAX]


class ActivityTrace:
    """一次生成的动作时间轴。每个 generation 一个实例。

    用 `threading.Lock` 而不是 asyncio 锁：`record()` 可能在工具线程里被调用
    （code_run 的回调），而聚合缓冲是可变状态。
    """

    __slots__ = ("generation_id", "turn_label", "_t0", "_buf", "_lock",
                 "_pending", "_last_flush")

    def __init__(self, generation_id: str = "", turn_label: str = "") -> None:
        self.generation_id = generation_id
        self.turn_label = turn_label
        self._t0 = time.monotonic()
        self._buf: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # 聚合缓冲按 (kind, agent) 分桶。
        # 为什么必须带 agent：4 个子代理并行时，若只按 kind 分桶，日志里全是
        # `action=thinking brief=+450 字`，看不出是哪个 agent 写的 ——
        # 而「谁在干活 / 谁卡住」正是多代理场景最需要的信息
        # （2026-11-20 真机 4 子代理演示抓出来的缺口）。
        self._pending: dict[tuple[str, str], int] = {}
        self._last_flush = self._t0

    def offset_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def record(self, action: str, *, name: str = "", detail: Any = None) -> None:
        """记一个动作。

        流式增量（`text_delta` / `thinking_delta`，含子代理前缀形式
        `sub.text_delta` / `sub.thinking_delta`）在这里被**自动路由**到聚合路径。

        为什么不要求调用方改用 `record_text`：那样的话
        `record("text_delta")` 会静默变成「每帧一条」，一次长回复写出几十万行，
        而调用方看不出自己用错了 API。宁可让这个类自己识别。

        为什么必须包含 `sub.` 前缀：首版只认裸名字，结果子代理的
        `sub.thinking_delta` 每帧一条 —— 一次实测里 60 秒内写了几百行，
        全部是 "I have enough from" 这种半句。聚合在这里必须覆盖全部流式形式。
        """
        if not enabled():
            return
        base = action[4:] if action.startswith("sub.") else action
        if base in ("text_delta", "thinking_delta"):
            # ★ detail 可能是 dict（`{"text": ...}`）也可能是**裸字符串**
            #   （接入侧传 `ev.get("text")`）。首版只处理 dict，
            #   于是真机 run2 里 5 万多字符全部被静默丢弃 ——
            #   日志看起来"聚合生效"，实际把数据丢光了。
            if isinstance(detail, dict):
                text = detail.get("text", "")
            else:
                text = detail if isinstance(detail, str) else ""
            # 子代理事件用 name 当 agent 归属；根代理固定 "root"。
            agent = name or ("root" if not action.startswith("sub.") else "sub")
            # ⚠️ kind 必须是 "thinking"，不能写成 "reasoning" ——
            # `_flush_locked` 拿 kind 当 action 名落盘，改名会让跨轮日志
            # 无法对比（2026-11-20 实测踩过：run1 是 thinking、run2 变成
            # reasoning，核验脚本对同一份文档算出两个数）。
            self.record_text(
                "thinking" if base == "thinking_delta" else "text",
                str(text), agent)
            return
        # 容器事件本身没有语义（它的内容由内部的 sub.* 事件表达），
        # 记一条空的只是噪音 —— 实测一次运行里 1/3 的行是这种空 brief。
        if action == "subagent_event":
            return
        now = time.monotonic()
        brief = _summarize(detail)
        if name:
            brief = f"{name} {brief}".strip()
        row = {"t": (now - self._t0) * 1000.0, "action": action,
               "brief": brief[: _BRIEF_MAX * 2]}
        with self._lock:
            self._flush_locked(now, force=True)
            self._buf.append(row)
        self._emit(row)

    def record_text(self, kind: str, text: str, agent: str = "root") -> None:
        """记一段正文/思考增量。按 (kind, agent) 累加，到窗口才落一行。"""
        if not enabled() or not text:
            return
        n = len(text)
        now = time.monotonic()
        key = (kind, agent or "root")
        with self._lock:
            self._pending[key] = self._pending.get(key, 0) + n
            if (now - self._last_flush) * 1000.0 >= _FLUSH_MS:
                self._flush_locked(now)

    def tick(self) -> None:
        """周期性调用（收到任意事件时）让聚合能按时落盘。"""
        if not enabled():
            return
        now = time.monotonic()
        with self._lock:
            if (now - self._last_flush) * 1000.0 >= _FLUSH_MS:
                self._flush_locked(now)

    def flush(self) -> None:
        """收尾：把聚合中的字数落成一行。"""
        if not enabled():
            return
        with self._lock:
            self._flush_locked(time.monotonic(), force=True)

    def _flush_locked(self, now: float, *, force: bool = False) -> None:
        if not force and (now - self._last_flush) * 1000.0 < _FLUSH_MS:
            return
        off = (now - self._t0) * 1000.0
        pending = dict(self._pending)
        self._pending.clear()
        for (kind, agent), n in pending.items():
            if not n:
                continue
            row = {"t": off, "action": kind, "brief": f"{agent} +{n} 字"}
            self._buf.append(row)
            self._emit(row)
        self._last_flush = now

    def _emit(self, row: dict[str, Any]) -> None:
        if _SINK is not None:
            _SINK.append(row)
            return
        try:
            logger.info("%s", _render(row, (self.generation_id or "-")[:12],
                                      self.turn_label or "-"))
        except Exception:  # noqa: BLE001 - 埋点绝不能拖垮请求
            pass

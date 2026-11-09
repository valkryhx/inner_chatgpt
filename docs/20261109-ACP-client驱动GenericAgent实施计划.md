# ACP Client 驱动 GenericAgent 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `inner_chatgpt` 内实现 ACP client，把本机 GenericAgent 作为可后台运行、可流式观察、可追问、可停止的外部子代理接入现有 root agent。

**Architecture:** 新增 `backend/app/acp/` 包（传输层 → 协议客户端 → 会话 → 管理器 → 5 个工具），挂到 `build_turn_runtime` 的每轮 registry；GA 的 `session/update` 复用现有 `EventBuffer` 与 `subagent_start/event/end` 事件，前端复用 `SubagentCard`。GA 以独立子进程运行，注册 `ProcessLease`，停止走「优雅 cancel → 硬杀」两级。

**Tech Stack:** Python 3.12 / asyncio / `subprocess.Popen` + 读线程 / JSON-RPC 2.0 over stdio / pytest；前端 React + TypeScript（仅一处标签）。

**Spec:** `docs/20261109-ACP-client驱动GenericAgent设计与实施.md`

## Global Constraints

- **不引入任何新依赖**（不用 `acp` 官方 SDK，不用 `anyio` 之外的库）；不 import `genericagent`。
- 所有工具失败必须返回 `[工具执行错误] ...` 文本，**绝不从 `Tool.call` 抛异常**（`backend/app/tools.py:59-75`）。
- SSE 事件名与字段保持不变（`backend/app/sse.py:1-12`）；不改 `subagent/host.py`、`subagent/runtime.py`、`subagent/store.py`、`team_*`、`agent_loop.py`、`tools.py`。
- 子进程管道全程二进制；不要 `text=True`（Windows 会做换行转换）。
- `ProcessLease.proc` 必须是带**异步 `wait()`** 的 `AsyncProcHandle`，不能裸 `Popen`。
- 文件 UTF-8 无 BOM、LF；Python 写文件用 `newline="\n"`。
- 所有命令在 `backend/` 目录下执行：`cd backend && python -m pytest tests -q`。
- TDD 红绿灯：先写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交。修 bug 时必须 stash 实现再跑一次确认重新变红。
- 测试不得访问真实网络、不得启动真实 GA；真 GA 只在 Task 12 的 opt-in E2E 中运行。

## Review Focus

这些是 spec 隐含、但最容易在真机上炸掉的输入/条件；每个都在下面对应任务里有测试：

1. **GA 的 stderr 洪泛**：bridge 对每条消息都 `eprint`（`bridge:181-191`）；不排空会写满 64KB 管道把 GA 卡死 → Task 2 测试 `test_stderr_flood_does_not_block`。
2. **GA 进程在 prompt 中途崩溃**：读线程 EOF 时所有 pending future 必须失败而不是永久挂起 → Task 3 测试 `test_prompt_fails_when_process_dies`。
3. **客户端断连/取消**：`_cleanup_turn` 的 await 可能被取消，必须有同步兜底杀进程 → Task 10 测试 `test_cleanup_stops_acp_synchronously_when_cancelled`。
4. **权限请求意外到达**（即使 `full_access`）：不自动应答会死锁 → Task 3 测试 `test_permission_request_is_auto_answered`。
5. **一个会话上并发 prompt**：本地必须先拒绝，不能把协议搞乱 → Task 3 测试 `test_second_prompt_is_rejected_locally`。
6. **Windows 粘包/拆包/CRLF**：一行拆成两次 write、两行粘在一起都要能解析 → Task 2 测试 `test_line_framing_handles_split_and_coalesced_writes`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `backend/app/acp/__init__.py` | 包入口，导出 `AcpSessionManager` |
| `backend/app/acp/config.py` | `AcpSettings` + `load_acp_settings(env)` |
| `backend/app/acp/transport.py` | `AsyncProcHandle`、`AcpProcess`（Popen、读写线程、行解析、stderr 环形缓冲） |
| `backend/app/acp/client.py` | `AcpClient`（JSON-RPC 关联、通知分发、权限自动应答） |
| `backend/app/acp/session.py` | `GaSession`（一次 GA 会话、事件映射、文本累积、结果） |
| `backend/app/acp/manager.py` | `AcpSessionManager`（会话表、配额、租约、GC、停止、事件发布） |
| `backend/app/acp/tools.py` | `build_acp_tools(manager)`（5 个 Tool） |
| `backend/tests/fixtures/fake_ga_bridge.py` | 假 ACP agent，按环境变量模拟 6 种行为 |
| `backend/tests/test_acp_config.py` | Task 1 |
| `backend/tests/test_acp_transport.py` | Task 2 |
| `backend/tests/test_acp_client.py` | Task 3 |
| `backend/tests/test_acp_session.py` | Task 4 |
| `backend/tests/test_acp_quota.py` | Task 5 |
| `backend/tests/test_acp_manager.py` | Task 6 |
| `backend/tests/test_acp_tools.py` | Task 7 |
| `backend/tests/test_acp_mount.py` | Task 8 |
| `backend/tests/test_acp_stop.py` | Tasks 9–10 |
| 修改 `backend/app/quota.py` | Task 5：两组信号量 |
| 修改 `backend/app/subagent/session.py` | Task 8：构造 manager + 注入工具 |
| 修改 `backend/app/runtime.py` | Task 9：per-generation manager 注册表 |
| 修改 `backend/app/routers/chat.py` | Task 10：merged events / stop 顺序 / 清理 |
| 修改 `frontend/src/components/SubagentCard.tsx` | Task 11：`ROLE_LABEL.ga` |

---

## Task 1: 配置加载

**Files:**
- Create: `backend/app/acp/__init__.py`
- Create: `backend/app/acp/config.py`
- Test: `backend/tests/test_acp_config.py`

**Interfaces:**
- Produces: `AcpSettings`（frozen dataclass）、`load_acp_settings(env: Mapping[str, str] | None = None) -> AcpSettings`、
  `build_child_env(settings: AcpSettings, base: Mapping[str, str] | None = None) -> dict[str, str]`
- `AcpSettings` 字段：`enabled: bool`、`command: list[str]`、`mode: str`、`permission: str`、`workdir_mode: str`、`max_active_user: int`、`max_active_global: int`、`max_open_user: int`、`prompt_timeout_s: float`、`drain_timeout_s: float`、`idle_ttl_s: float`、`stderr_lines: int`、`env_allow: tuple[str, ...]`、`mcp_config: str`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_acp_config.py
from app.acp.config import load_acp_settings


def test_disabled_without_command():
    s = load_acp_settings({})
    assert s.enabled is False
    assert s.command == []


def test_python_module_command():
    s = load_acp_settings({"INNER_ACP_GA_PYTHON": r"C:\anaconda3\python.exe"})
    assert s.enabled is True
    assert s.command == [r"C:\anaconda3\python.exe", "-m",
                         "frontends.acp.genericagent_acp_bridge"]


def test_explicit_json_command_wins():
    s = load_acp_settings({
        "INNER_ACP_GA_PYTHON": "ignored",
        "INNER_ACP_GA_COMMAND": '["C:/ga/python.exe","-m","x"]',
    })
    assert s.command == ["C:/ga/python.exe", "-m", "x"]


def test_defaults():
    s = load_acp_settings({"INNER_ACP_GA_PYTHON": "python"})
    assert (s.mode, s.permission, s.workdir_mode) == ("full_access", "allow", "workspace")
    assert (s.max_active_user, s.max_active_global, s.max_open_user) == (4, 80, 8)
    assert (s.prompt_timeout_s, s.drain_timeout_s, s.idle_ttl_s) == (1800.0, 300.0, 600.0)


def test_env_allow_parsed():
    s = load_acp_settings({"INNER_ACP_GA_PYTHON": "python", "INNER_ACP_ENV_ALLOW": "FOO, BAR"})
    assert s.env_allow == ("FOO", "BAR")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_config.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.acp'`

- [ ] **Step 3: 最小实现**

`backend/app/acp/__init__.py` 只写一行 docstring。`backend/app/acp/config.py`：

- `env` 缺省时用 `os.environ`。
- `INNER_ACP_GA_COMMAND` 以 `[` 开头则 `json.loads`，否则 `shlex.split(value, posix=False)`；该变量非空时优先于 `INNER_ACP_GA_PYTHON`。
- 仅 `INNER_ACP_GA_PYTHON` 时命令为 `[python, "-m", "frontends.acp.genericagent_acp_bridge"]`。
- 整数/浮点用 `int(env.get(name, default))` 风格的容错包装，非法值回落默认。
- `env_allow`：逗号分隔、strip、去空。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_config.py -q`
Expected: 5 passed

- [ ] **Step 5: 子进程环境白名单（同一个红绿循环）**

先加失败测试（追加到 `test_acp_config.py`）：

```python
def test_child_env_whitelist_and_proxy():
    from app.acp.config import build_child_env
    base = {"PATH": "p", "APPDATA": "a", "LOCALAPPDATA": "l", "HTTPS_PROXY": "proxy",
            "INNER_SECRET": "no", "GA_MCP_CONFIG": "m.json"}
    env = build_child_env(load_acp_settings({"INNER_ACP_GA_PYTHON": "python"}), base)
    assert env["APPDATA"] == "a"
    assert env["HTTPS_PROXY"] == "proxy"
    assert "INNER_SECRET" not in env
    assert env["GA_MCP_CONFIG"] == "m.json"


def test_child_env_allow_list_and_mcp_override():
    from app.acp.config import build_child_env
    s = load_acp_settings({"INNER_ACP_GA_PYTHON": "python",
                           "INNER_ACP_ENV_ALLOW": "FOO",
                           "INNER_ACP_GA_MCP_CONFIG": "empty.json"})
    env = build_child_env(s, {"FOO": "1", "BAR": "2"})
    assert env["FOO"] == "1"
    assert "BAR" not in env
    assert env["GA_MCP_CONFIG"] == "empty.json"
```

Run: `cd backend && python -m pytest tests/test_acp_config.py -q`
Expected: FAIL，`ImportError: cannot import name 'build_child_env'`

实现：

- 固定白名单：`PATH`、`SystemRoot`、`SystemDrive`、`windir`、`TEMP`、`TMP`、`USERPROFILE`、
  `HOMEDRIVE`、`HOMEPATH`、`APPDATA`、`LOCALAPPDATA`、`LANG`、`PYTHONIOENCODING`；
- 可选透传：`HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY`、`GA_CONFIG_DIR`、`GA_LLM_CONFIG`、`GA_MCP_CONFIG`（存在才带）；
- `settings.env_allow` 里的名字同样透传；
- `settings.mcp_config` 非空时强制覆盖 `GA_MCP_CONFIG`；
- 只从 `base`（缺省 `os.environ`）取值，**不包含任何其它 `INNER_*`**。

Run: `cd backend && python -m pytest tests/test_acp_config.py -q`
Expected: 7 passed

- [ ] **Step 6: 提交**

```bash
cd backend && git add app/acp/__init__.py app/acp/config.py tests/test_acp_config.py
git commit -m "feat(acp): add ACP settings loader and child env whitelist"
```

---

## Task 2: 假 bridge + 传输层

**Files:**
- Create: `backend/tests/fixtures/fake_ga_bridge.py`
- Create: `backend/app/acp/transport.py`
- Test: `backend/tests/test_acp_transport.py`

**Interfaces:**
- Produces:
  - `AcpTransportClosed(RuntimeError)`
  - `AsyncProcHandle(proc: subprocess.Popen)`：`pid`、`returncode`、`kill()`、`async wait() -> int`
  - `decode_line(raw: bytes) -> dict | None`
  - `AcpProcess(command: Sequence[str], *, cwd: str, env: Mapping[str, str], stderr_lines: int = 200)`：
    `start() -> None`、`set_message_handler(handler: Callable[[dict], None])`、
    `set_close_handler(handler: Callable[[BaseException], None])`、`async send(msg: dict) -> None`、
    `async close(grace_s: float = 1.0) -> None`、`proc -> AsyncProcHandle | None`、
    `stderr_tail(n: int = 20) -> list[str]`
- 消息处理函数在**事件循环线程**里被调用；读线程只负责投递。

- [ ] **Step 1: 写假 bridge（测试支撑代码）**

```python
# backend/tests/fixtures/fake_ga_bridge.py
"""Minimal ACP agent. Env:
FAKE_GA_MODE = chunks|tools|permission|hang|crash|malformed
FAKE_GA_STDERR_LINES = int (flood stderr *before* reading stdin)
FAKE_GA_LOG = path (append every received message as one JSON line)
"""
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_GA_MODE", "chunks")
LOG = os.environ.get("FAKE_GA_LOG")


def log(obj):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid, "update": upd}})


def main():
    for _ in range(int(os.environ.get("FAKE_GA_STDERR_LINES", "0") or 0)):
        sys.stderr.write("[FAKE] noise line to fill the pipe\n")
    sys.stderr.flush()
    pending = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        log(msg)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        sid = params.get("sessionId")
        if MODE == "crash" and method == "session/prompt":
            os._exit(3)
        if MODE == "malformed":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1, "agentCapabilities": {},
                "agentInfo": {"name": "fake-ga", "version": "0.0.0"}, "authMethods": []}}
            if MODE == "split":
                payload = json.dumps(resp, ensure_ascii=False)
                sys.stdout.write(payload[:12])
                sys.stdout.flush()
                time.sleep(0.05)
                sys.stdout.write(payload[12:] + "\n")
                sys.stdout.flush()
            else:
                send(resp)
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "sessionId": "ga_fake_1",
                "modes": {"currentModeId": "full_access", "availableModes": []},
                "configOptions": None}})
        elif method == "session/set_mode":
            send({"jsonrpc": "2.0", "id": rid, "result": {"modeId": params.get("modeId")}})
        elif method == "session/prompt":
            if MODE == "hang":
                pending[sid] = rid
            elif MODE == "permission":
                send({"jsonrpc": "2.0", "id": "perm_fake",
                      "method": "session/request_permission",
                      "params": {"sessionId": sid,
                                 "toolCall": {"toolCallId": "t1", "title": "file_write",
                                              "kind": "edit", "status": "pending"},
                                 "options": [{"optionId": "allow_once", "kind": "allow_once"},
                                             {"optionId": "reject_once", "kind": "reject_once"}]}})
                pending[sid] = rid
            else:
                if MODE == "tools":
                    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1",
                                 "title": "file_read", "kind": "read", "status": "in_progress",
                                 "rawInput": {"preview": "a.py"}})
                    update(sid, {"sessionUpdate": "agent_message_chunk",
                                 "content": {"type": "text", "text": "reading..."}})
                    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                                 "status": "completed",
                                 "content": [{"type": "content",
                                              "content": {"type": "text", "text": "ok"}}]})
                    update(sid, {"sessionUpdate": "agent_message_chunk",
                                 "content": {"type": "text", "text": "FINAL"}})
                else:
                    for chunk in ("hello ", "world"):
                        update(sid, {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": chunk}})
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            for s, prid in list(pending.items()):
                send({"jsonrpc": "2.0", "id": prid, "result": {"stopReason": "cancelled"}})
                pending.pop(s)
        elif method == "session/close":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None and method is None:
            for s, prid in list(pending.items()):
                update(s, {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "answered"}})
                send({"jsonrpc": "2.0", "id": prid, "result": {"stopReason": "end_turn"}})
                pending.pop(s)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 写失败测试**

```python
# backend/tests/test_acp_transport.py
import asyncio
import os
import sys
from pathlib import Path

from app.acp.transport import AcpProcess, AsyncProcHandle, decode_line

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


def run(coro):
    return asyncio.run(coro)


def start(mode="chunks", **env):
    proc = AcpProcess([sys.executable, FIXTURE], cwd=str(Path.cwd()),
                      env={**os.environ, "FAKE_GA_MODE": mode, **env})
    proc.start()
    return proc


def test_decode_line_strips_crlf_and_ignores_garbage():
    assert decode_line(b'{"a":1}\r\n') == {"a": 1}
    assert decode_line(b"not json\n") is None
    assert decode_line(b"\n") is None


def test_roundtrip_response():
    async def scenario():
        p = start()
        got = asyncio.get_running_loop().create_future()
        p.set_message_handler(lambda m: got.done() or got.set_result(m))
        try:
            await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            return await asyncio.wait_for(got, 10)
        finally:
            await p.close()

    msg = run(scenario())
    assert msg["id"] == 1
    assert msg["result"]["agentInfo"]["name"] == "fake-ga"


def test_split_write_is_reassembled():
    async def scenario():
        p = start("split")
        got = asyncio.get_running_loop().create_future()
        p.set_message_handler(lambda m: got.done() or got.set_result(m))
        try:
            await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            return await asyncio.wait_for(got, 10)
        finally:
            await p.close()

    assert run(scenario())["result"]["agentInfo"]["name"] == "fake-ga"


def test_stderr_flood_does_not_block():
    async def scenario():
        p = start(stderr_lines=20000)   # > 64KB of stderr before reading stdin
        got = asyncio.get_running_loop().create_future()
        p.set_message_handler(lambda m: got.done() or got.set_result(m))
        try:
            await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            return await asyncio.wait_for(got, 10)
        finally:
            await p.close()

    assert run(scenario())["id"] == 1


def test_close_terminates_process_and_wait_is_async():
    async def scenario():
        p = start()
        handle = p.proc
        assert isinstance(handle, AsyncProcHandle)
        await p.close(grace_s=0.2)
        return await asyncio.wait_for(handle.wait(), 5)

    assert isinstance(run(scenario()), int)


def test_unknown_line_does_not_kill_reader():
    async def scenario():
        p = start("malformed")
        msgs = []
        done = asyncio.get_running_loop().create_future()

        def handler(m):
            msgs.append(m)
            if not done.done():
                done.set_result(True)

        p.set_message_handler(handler)
        try:
            await p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            await asyncio.wait_for(done, 10)
            assert msgs[0]["id"] == 1
        finally:
            await p.close()

    run(scenario())
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_transport.py -q`
Expected: FAIL，`ImportError: cannot import name 'AcpProcess'`

- [ ] **Step 4: 实现 `transport.py`**

- `decode_line`：`raw.decode("utf-8", "replace").strip()`，空串或 `json.loads` 失败返回 `None`，非 dict 也返回 `None`。
- `AcpProcess.start()`：`subprocess.Popen(command, cwd=cwd, env=dict(env), stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0, creationflags=(0x08000000 if os.name == "nt" else 0))`。
- stdout 读线程：`for raw in iter(proc.stdout.readline, b"")` → `decode_line` → `loop.call_soon_threadsafe(self._dispatch, msg)`；循环结束（EOF）时 `call_soon_threadsafe(self._closed, AcpTransportClosed(...))`。
- stderr 读线程：**无条件持续读取**，每行解码后压入 `collections.deque(maxlen=stderr_lines)`；绝不因队列满而停止。
- `send()`：`self._write_lock` 串行化，`await asyncio.to_thread(self._write, payload)`；进程已退出抛 `AcpTransportClosed`。
- `close()`：`stdin.close()` → `await asyncio.wait_for(handle.wait(), grace_s)` 超时则 `handle.kill()` → 再等 2s。
- `_dispatch` 与 `_closed` 都在事件循环线程执行；未注册 handler 时静默丢弃。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_transport.py -q`
Expected: 6 passed

- [ ] **Step 6: 提交**

```bash
cd backend && git add app/acp/transport.py tests/fixtures/fake_ga_bridge.py tests/test_acp_transport.py
git commit -m "feat(acp): add stdio transport with stderr drain and async proc handle"
```

---

## Task 3: 协议客户端

**Files:**
- Modify: `backend/tests/fixtures/fake_ga_bridge.py`（加 `error` 模式）
- Create: `backend/app/acp/client.py`
- Test: `backend/tests/test_acp_client.py`

**Interfaces:**
- Consumes: `AcpProcess`、`AcpTransportClosed`（Task 2）
- Produces:
  - `AcpError(RuntimeError)`：`code: int`、`message: str`、`data: Any`
  - `AcpBusyError(RuntimeError)`
  - `AcpClient(process: AcpProcess, *, permission: str = "allow", on_update: Callable[[str, dict], None] | None = None)`
  - 方法：`async start() -> dict`、`async new_session(cwd: str) -> dict`、
    `async set_mode(session_id: str, mode_id: str) -> dict`、
    `async prompt(session_id: str, text: str, *, timeout_s: float) -> dict`、
    `async cancel(session_id: str) -> None`、`async close_session(session_id: str) -> dict`、
    `async close() -> None`
  - 属性：`permission_requests: list[dict]`、`agent_info: dict`

- [ ] **Step 1: 给假 bridge 加 `error` 模式**

在 `main()` 的 `elif method == "session/prompt":` 分支最前面加：

```python
            if MODE == "error":
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32603, "message": "fake failure"}})
                continue
```

- [ ] **Step 2: 写失败测试**

```python
# backend/tests/test_acp_client.py
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.acp.client import AcpBusyError, AcpClient, AcpError
from app.acp.transport import AcpProcess, AcpTransportClosed

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


def run(coro):
    return asyncio.run(coro)


async def make(mode="chunks", permission="allow", **env):
    p = AcpProcess([sys.executable, FIXTURE], cwd=str(Path.cwd()),
                   env={**os.environ, "FAKE_GA_MODE": mode, **env})
    p.start()
    updates = []
    c = AcpClient(p, permission=permission, on_update=lambda sid, u: updates.append(u))
    await c.start()
    return p, c, updates


def test_start_returns_agent_info():
    async def scenario():
        p, c, _ = await make()
        try:
            return c.agent_info["name"]
        finally:
            await c.close()

    assert run(scenario()) == "fake-ga"


def test_update_notifications_are_dispatched():
    async def scenario():
        p, c, updates = await make("chunks")
        try:
            s = await c.new_session(str(Path.cwd()))
            await c.prompt(s["sessionId"], "hi", timeout_s=10)
            return [u["content"]["text"] for u in updates]
        finally:
            await c.close()

    assert run(scenario()) == ["hello ", "world"]


def test_permission_request_is_auto_answered():
    async def scenario():
        p, c, updates = await make("permission", permission="allow")
        try:
            s = await c.new_session(str(Path.cwd()))
            r = await c.prompt(s["sessionId"], "write a file", timeout_s=10)
            return r["stopReason"], c.permission_requests, updates
        finally:
            await c.close()

    reason, requests, updates = run(scenario())
    assert reason == "end_turn"
    assert len(requests) == 1
    assert updates[-1]["content"]["text"] == "answered"


def test_permission_deny_is_sent(tmp_path):
    log = tmp_path / "msgs.jsonl"

    async def scenario():
        p, c, _ = await make("permission", permission="deny", FAKE_GA_LOG=str(log))
        try:
            s = await c.new_session(str(Path.cwd()))
            await c.prompt(s["sessionId"], "write a file", timeout_s=10)
        finally:
            await c.close()

    run(scenario())
    sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    answers = [m for m in sent if m.get("id") == "perm_fake" and m.get("result")]
    assert answers and answers[0]["result"]["outcome"]["optionId"] == "reject_once"


def test_second_prompt_is_rejected_locally():
    async def scenario():
        p, c, _ = await make("hang")
        try:
            s = await c.new_session(str(Path.cwd()))
            first = asyncio.create_task(c.prompt(s["sessionId"], "one", timeout_s=30))
            await asyncio.sleep(0.2)
            with pytest.raises(AcpBusyError):
                await c.prompt(s["sessionId"], "two", timeout_s=5)
            await c.cancel(s["sessionId"])
            return await first
        finally:
            await c.close()

    assert run(scenario())["stopReason"] == "cancelled"


def test_prompt_fails_when_process_dies():
    async def scenario():
        p, c, _ = await make("crash")
        try:
            s = await c.new_session(str(Path.cwd()))
            with pytest.raises(AcpTransportClosed):
                await c.prompt(s["sessionId"], "boom", timeout_s=10)
        finally:
            await c.close()

    run(scenario())


def test_jsonrpc_error_is_raised():
    async def scenario():
        p, c, _ = await make("error")
        try:
            s = await c.new_session(str(Path.cwd()))
            with pytest.raises(AcpError) as exc:
                await c.prompt(s["sessionId"], "x", timeout_s=10)
            return exc.value.code
        finally:
            await c.close()

    assert run(scenario()) == -32603
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_client.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.acp.client'`

- [ ] **Step 4: 实现 `client.py`**

- `start()`：`initialize` 的 params 固定 `{"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "inner_chatgpt", "version": "1.0"}}`；保存 `agent_info = result["agentInfo"]`。
- 请求关联：`_next_id` 自增；`_pending: dict[int, asyncio.Future]`；`_request(method, params, timeout_s)` 发消息后 `wait_for`，超时取消 future 并抛 `asyncio.TimeoutError`。
- `_dispatch(msg)`：
  - `method` 且 `id` 非空 → 请求：`session/request_permission` 记录到 `permission_requests`，按 `permission` 回 `allow_once` / `reject_once`；其它方法回 `-32601`。
  - `method` 且无 `id` → 通知：`session/update` 取 `params.sessionId` 与 `params.update` 调 `on_update`（回调异常吞掉并记 stderr）。
  - 无 `method` 且 `id` 非空 → 响应：有 `error` 则 `set_exception(AcpError(...))`，否则 `set_result(result)`。
- `_on_close(exc)`：把所有 pending future `set_exception(exc)`。
- `prompt()`：进入时若 `self._active_prompt` 非空抛 `AcpBusyError`；用 `try/finally` 清空（取消/超时也要清）。
- `cancel()`：发通知，无 `id`。
- `close()`：`await process.close()`。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_client.py -q`
Expected: 7 passed

- [ ] **Step 6: 提交**

```bash
cd backend && git add app/acp/client.py tests/test_acp_client.py tests/fixtures/fake_ga_bridge.py
git commit -m "feat(acp): add JSON-RPC client with permission auto-answer"
```

---

## Task 4: 会话与事件映射

**Files:**
- Create: `backend/app/acp/session.py`
- Test: `backend/tests/test_acp_session.py`

**Interfaces:**
- Consumes: `AcpClient`（Task 3）、`AcpSettings`（Task 1）
- Produces:
  - `PromptResult`（dataclass）：`status: str`（`completed|errored|stopped`）、`result: str`、`full_text: str`、`stop_reason: str`、`tool_calls: int`、`error: str`
  - `GaSession(agent_id: str, title: str, task: str, *, settings, cwd: str, env: Mapping[str, str], publish: Callable[[dict], None])`
  - 方法：`async start() -> None`、`async prompt(text: str, *, prompt_seq: int, timeout_s: float) -> PromptResult`、
    `async stop() -> None`、`async close() -> None`、`snapshot() -> dict`、`tail(since_seq: int, limit: int = 8) -> list[dict]`
  - 属性：`status: str`、`last_seq: int`、`cwd: str`、`process_handle: AsyncProcHandle | None`

**事件映射规则（必须与 spec §8.1/§8.3 一致）：**

| ACP update | buffer 事件 |
|---|---|
| `agent_message_chunk` | `subagent_event` / `text_delta` |
| `tool_call` | `subagent_event` / `tool_start`（id=`toolCallId`，name=`title`，input=`rawInput`） |
| `tool_call_update` | `subagent_event` / `tool_result`（content=文本或 status，is_error=status in failed 集合） |

- 卡片 id = `f"{agent_id}#{prompt_seq}"`；每次 `prompt()` 先发 `subagent_start`。
- `full_text` = 所有文本 chunk 拼接；`result` = 最后一个工具事件之后的文本，空则退回 `full_text`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_acp_session.py
import asyncio
import json
import os
import sys
from pathlib import Path

from app.acp.config import load_acp_settings
from app.acp.session import GaSession

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


def run(coro):
    return asyncio.run(coro)


def settings(mode="chunks"):
    return load_acp_settings({
        "INNER_ACP_GA_COMMAND": json.dumps([sys.executable, FIXTURE]),
        "FAKE_GA_MODE": mode,
    })


def make(mode, events):
    env = {**os.environ, "FAKE_GA_MODE": mode}
    return GaSession("ga_test", "T", "task", settings=settings(mode),
                     cwd=str(Path.cwd()), env=env, publish=events.append)


def test_text_chunks_map_to_events_and_full_text():
    async def scenario():
        events = []
        s = make("chunks", events)
        await s.start()
        try:
            r = await s.prompt("hi", prompt_seq=1, timeout_s=10)
            return r, events, s
        finally:
            await s.close()

    r, events, s = run(scenario())
    assert r.status == "completed"
    assert r.full_text == "hello world"
    assert r.result == "hello world"
    assert [e["agent_id"] for e in events if e["type"] == "subagent_event"] == ["ga_test#1"] * 2
    assert [e["event"]["type"] for e in events if e["type"] == "subagent_event"] == [
        "text_delta", "text_delta"]


def test_tool_events_map_and_result_is_tail():
    async def scenario():
        events = []
        s = make("tools", events)
        await s.start()
        try:
            r = await s.prompt("hi", prompt_seq=1, timeout_s=10)
            return r, [e["event"] for e in events if e["type"] == "subagent_event"]
        finally:
            await s.close()

    r, evs = run(scenario())
    assert r.tool_calls == 1
    assert r.full_text == "reading...FINAL"
    assert r.result == "FINAL"
    assert [e["type"] for e in evs] == ["tool_start", "text_delta", "tool_result", "text_delta"]
    assert evs[0]["name"] == "file_read"
    assert evs[2]["is_error"] is False


def test_second_prompt_gets_new_card_id():
    async def scenario():
        events = []
        s = make("chunks", events)
        await s.start()
        try:
            await s.prompt("one", prompt_seq=1, timeout_s=10)
            await s.prompt("two", prompt_seq=2, timeout_s=10)
            return [e["agent_id"] for e in events if e["type"] == "subagent_start"]
        finally:
            await s.close()

    assert run(scenario()) == ["ga_test#1", "ga_test#2"]


def test_stop_marks_stopped():
    async def scenario():
        events = []
        s = make("hang", events)
        await s.start()
        try:
            task = asyncio.create_task(s.prompt("one", prompt_seq=1, timeout_s=30))
            await asyncio.sleep(0.2)
            await s.stop()
            r = await asyncio.wait_for(task, 10)
            return r.status, s.status
        finally:
            await s.close()

    assert run(scenario()) == ("stopped", "stopped")


def test_process_death_marks_errored():
    async def scenario():
        events = []
        s = make("crash", events)
        await s.start()
        try:
            r = await s.prompt("boom", prompt_seq=1, timeout_s=10)
            return r.status, s.status, s.snapshot()
        finally:
            await s.close()

    status, session_status, snap = run(scenario())
    assert status == "errored"
    assert session_status == "errored"
    assert snap["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_session.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.acp.session'`

- [ ] **Step 3: 实现 `session.py`**

- `start()`：建 `AcpProcess` → `AcpClient(process, permission=settings.permission, on_update=self._on_update)` → `await client.start()` → `new_session(cwd)` → 若 `settings.mode != modes.currentModeId` 则 `set_mode`。
- `_on_update(session_id, update)`：按上表映射并 `publish`；同时追加事件日志 `{"seq", "kind", "name", "text"}`（`last_seq` 自增，最多保留 2000 条）。`tool_call` 时 `tool_calls += 1`；`tool_call_update` 完成后清空 `_tail`。
- `prompt()`：`status="running"`；`publish(subagent_start)`；`await client.prompt(...)`；成功 `status="completed"`；`AcpTransportClosed` → `errored`；`asyncio.TimeoutError` → 先 `cancel` 再 `stopped`。**无论哪条路径都发一条 `subagent_end`**（有文本事件时不带 `result`，无文本时 `result="（GA 未返回文本）"`）。
- `stop()`：`status="stopped"`；`await client.cancel(session_id)`；已死进程忽略异常。
- `close()`：`await client.close()`；进程句柄保留供租约使用到 `close_all`。
- `snapshot()`：`{"agent_id","status","result","full_text","tool_calls","prompts","error","last_seq"}`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_session.py -q`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/acp/session.py tests/test_acp_session.py
git commit -m "feat(acp): map GA session updates to subagent events"
```

---

## Task 5: 配额

**Files:**
- Modify: `backend/app/quota.py`（`Limits`、`QuotaManager.__init__`、新增 `acp_active`）
- Test: `backend/tests/test_acp_quota.py`

**Interfaces:**
- Produces: `QuotaManager.acp_active(user_id: str, timeout_s: float = 2.0)` 异步上下文管理器，超时抛 `QuotaExceeded`。
- `Limits` 新增字段：`user_acp_active`（默认 4）、`global_acp_active`（默认 80）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_acp_quota.py
import asyncio

import pytest

from app.quota import Limits, QuotaExceeded, QuotaManager


def test_second_acp_lease_is_rejected_for_same_user():
    async def scenario():
        q = QuotaManager(Limits(user_acp_active=1, global_acp_active=2))
        async with q.acp_active("u1", timeout_s=0.05):
            with pytest.raises(QuotaExceeded):
                async with q.acp_active("u1", timeout_s=0.05):
                    pass

    asyncio.run(scenario())


def test_other_user_is_not_blocked():
    async def scenario():
        q = QuotaManager(Limits(user_acp_active=1, global_acp_active=2))
        async with q.acp_active("u1", timeout_s=0.05):
            async with q.acp_active("u2", timeout_s=0.05):
                return True

    assert asyncio.run(scenario())


def test_global_limit():
    async def scenario():
        q = QuotaManager(Limits(user_acp_active=1, global_acp_active=1))
        async with q.acp_active("u1", timeout_s=0.05):
            with pytest.raises(QuotaExceeded):
                async with q.acp_active("u2", timeout_s=0.05):
                    pass

    asyncio.run(scenario())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_quota.py -q`
Expected: FAIL，`AttributeError: 'QuotaManager' object has no attribute 'acp_active'`

- [ ] **Step 3: 实现**

- `Limits` 加两个 `_env_int` 字段（`INNER_ACP_MAX_ACTIVE_USER` / `INNER_ACP_MAX_ACTIVE_GLOBAL`）。
- `__init__` 加 `self._user_acp_active: dict[str, asyncio.Semaphore] = {}`、`self._global_acp_active = asyncio.Semaphore(self.limits.global_acp_active)`。
- `acp_active` 完全照抄现有 `mcp_call` 的结构（`backend/app/quota.py:150-163`），信号量列表为
  `[global, user]`，用 `self._acquire_many(sems, timeout_s)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_quota.py -q`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/quota.py tests/test_acp_quota.py
git commit -m "feat(quota): add ACP active-prompt semaphores"
```

---

## Task 6: 会话管理器

**Files:**
- Create: `backend/app/acp/manager.py`
- Modify: `backend/app/acp/transport.py`（加同步 `kill_tree_now(handle)`）
- Test: `backend/tests/test_acp_manager.py`

**Interfaces:**
- Consumes: `GaSession`（Task 4）、`AcpSettings`（Task 1）、`QuotaManager.acp_active`（Task 5）、
  `ProcessLease/register/unregister`（`backend/app/process_registry.py`）
- Produces:
  - `AcpLimitError(RuntimeError)`
  - `AcpSessionManager(*, ctx: AgentContext, buffer: EventBuffer, settings: AcpSettings, quota=None)`
  - `async spawn(*, task: str, title: str = "", mode: str = "", timeout_s: float = 0.0) -> dict`
  - `async wait(agent_id: str | None, *, timeout_s: float = 60.0, since_seq: int = 0) -> dict`
  - `async result(agent_id: str, *, max_chars: int = 20000, full: bool = False, close: bool = False) -> dict`
  - `async send(agent_id: str, message: str) -> dict`
  - `async stop(agent_id: str | None) -> dict`
  - `has_active() -> bool`、`stop_all_now() -> None`、`async stop_all() -> int`、
    `async close_all() -> None`、`async stop_generation() -> int`
  - 公开属性 `quota`（测试可替换；缺省 `get_quota()`）
- 返回值形状见 spec §7.2；所有异常由上层工具转成文本，manager 自身可以抛。

- [ ] **Step 1: 在 `transport.py` 加同步杀树**

```python
def kill_tree_now(handle: AsyncProcHandle) -> None:
    """Synchronous best-effort kill used from cancel-safe cleanup paths."""
    if handle is None or handle.returncode is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(handle.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(os.getpgid(handle.pid), 9)
        except OSError:
            handle.kill()
```

- [ ] **Step 2: 写失败测试**

```python
# backend/tests/test_acp_manager.py
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from app.acp.config import load_acp_settings
from app.acp.manager import AcpLimitError, AcpSessionManager
from app.quota import Limits, QuotaExceeded, QuotaManager
from app.subagent.context import AgentContext


class RecordingBuffer:
    def __init__(self):
        self.events = []

    def put(self, ev):
        self.events.append(ev)

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_ga_bridge.py")


def run(coro):
    return asyncio.run(coro)


def make(**extra):
    env = {
        "INNER_ACP_GA_COMMAND": json.dumps([sys.executable, FIXTURE]),
        "INNER_ACP_PROMPT_TIMEOUT_S": "30",
        **extra,
    }
    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    buf = RecordingBuffer()
    return AcpSessionManager(ctx=ctx, buffer=buf, settings=load_acp_settings(env)), buf


def test_spawn_and_wait_completes(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="say hi")
            await mgr.wait(out["agent_id"], timeout_s=10)
            return await mgr.result(out["agent_id"]), mgr.has_active()
        finally:
            await mgr.close_all()

    r, active = run(scenario())
    assert r["status"] == "completed"
    assert r["result"] == "hello world"
    assert active is False


def test_spawn_uses_user_workspace(tmp_path, monkeypatch):
    from app import workspace
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="hi")
            return mgr._sessions[out["agent_id"]].cwd
        finally:
            await mgr.close_all()

    assert run(scenario()).startswith(str(tmp_path / "ws"))


def test_spawn_returns_immediately(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr, _ = make()
        try:
            t0 = time.monotonic()
            out = await mgr.spawn(task="long")
            elapsed = time.monotonic() - t0
            active = mgr.has_active()
            await mgr.stop(out["agent_id"])
            return elapsed, active
        finally:
            await mgr.close_all()

    elapsed, active = run(scenario())
    assert elapsed < 5
    assert active is True


def test_quota_rejects_second_prompt(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr, _ = make()
        mgr.quota = QuotaManager(Limits(user_acp_active=1, global_acp_active=2))
        try:
            await mgr.spawn(task="one")
            with pytest.raises(QuotaExceeded):
                await mgr.spawn(task="two")
        finally:
            await mgr.close_all()

    run(scenario())


def test_open_session_cap(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, _ = make(INNER_ACP_MAX_OPEN_USER="1")
        try:
            await mgr.spawn(task="one")
            await asyncio.sleep(0.3)
            with pytest.raises(AcpLimitError):
                await mgr.spawn(task="two")
        finally:
            await mgr.close_all()

    run(scenario())


def test_stop_all_now_kills_process_synchronously(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="long")
            handle = mgr._sessions[out["agent_id"]].process_handle
            mgr.stop_all_now()
            for _ in range(50):
                if handle.returncode is not None:
                    return True
                await asyncio.sleep(0.1)
            return False
        finally:
            await mgr.close_all()

    assert run(scenario()) is True


def test_crash_marks_errored(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "crash")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="boom")
            await mgr.wait(out["agent_id"], timeout_s=10)
            return await mgr.result(out["agent_id"])
        finally:
            await mgr.close_all()

    assert run(scenario())["status"] == "errored"


def test_send_creates_new_card(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, buf = make()
        try:
            out = await mgr.spawn(task="one")
            aid = out["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            await mgr.send(aid, "two")
            await mgr.wait(aid, timeout_s=10)
            return [e["agent_id"] for e in buf.events if e["type"] == "subagent_start"]
        finally:
            await mgr.close_all()

    cards = run(scenario())
    assert len(cards) == 2
    assert cards[0] == cards[1].replace("#2", "#1")
    assert cards[0].endswith("#1") and cards[1].endswith("#2")
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_manager.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.acp.manager'`

- [ ] **Step 4: 实现 `manager.py`**

- `spawn()`：
  1. 打开会话数 `len(self._sessions) >= settings.max_open_user` → 抛 `AcpLimitError`；
  2. `await self.quota.acp_active(ctx.user_id, timeout_s=2.0)`（用 `AsyncExitStack` 持有）；
  3. `cwd` 由服务端决定（**绝不接受模型输入**）：
     `cwd = user_workspace(ctx.user_id)`；若 `settings.workdir_mode == "subdir"`，
     `cwd = Path(cwd) / ".ga" / ctx.generation_id`；`Path(cwd).mkdir(parents=True, exist_ok=True)`；
  4. `agent_id = f"ga_{uuid.uuid4().hex[:8]}"`；`GaSession(..., cwd=str(cwd), env=build_child_env(settings))`；
     `await session.start()`；
  5. 建 `ProcessLease(ctx.user_id, ctx.session_id, ctx.generation_id, agent_id)`，`lease.proc = session.process_handle`，`await register(lease)`；
  6. `task = asyncio.create_task(self._run_prompt(...))`；返回
     `{"agent_id", "status": "running", "note": "用 ga_wait / ga_result 收结果；本轮结束会停止未完成任务"}`；
  7. 第 4 步失败时释放配额并抛可读异常。
- `_run_prompt()`：`await session.prompt(...)`；`finally` 释放配额、把 task 从 `_prompts` 移除。
- `wait()`：`agent_id` 为 None 时目标=全部；用 `asyncio.wait(tasks, timeout=...)`；超时返回 `timed_out=True`；每项含 `snapshot()` 与 `tail(since_seq)`。
- `result()`：`snapshot()`；`full` 决定用 `full_text` 还是 `result`；截断到 `max_chars` 并标注；`close=True` 时 `await self._close_session(agent_id)`。
- `send()`：无 active prompt 才允许；`prompt_seq += 1`；重新 acquire 配额；起新 task。
- `stop()`：`session.stop()`；等 task ≤2s；未结束 → `kill_tree_now(handle)`；状态 `stopped`。
- `stop_all_now()`：同步遍历 `_sessions`，`kill_tree_now`，标记 `stopped`；**不 await**。
- `stop_all()` / `stop_generation()`：逐个 `await self.stop(aid)`，返回数量。
- `close_all()`：`stop_all()` → `await session.close()` → `await unregister(lease)` → 清表。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_manager.py -q`
Expected: 8 passed

- [ ] **Step 6: 提交**

```bash
cd backend && git add app/acp/manager.py app/acp/transport.py tests/test_acp_manager.py
git commit -m "feat(acp): add session manager with quota, leases and stop cascade"
```

---

## Task 7: 预检与工具组

**Files:**
- Modify: `backend/app/acp/manager.py`（新增 `preflight()`，`spawn()` 首次调用）
- Create: `backend/app/acp/tools.py`
- Test: `backend/tests/test_acp_tools.py`

**Interfaces:**
- Produces:
  - `AcpUnavailableError(RuntimeError)`
  - `AcpSessionManager.preflight() -> str | None`：返回 bridge 文件路径（自定义命令时返回 `None`）
  - `build_acp_tools(manager: AcpSessionManager) -> list[Tool]`
- 工具名固定：`ga_spawn`、`ga_wait`、`ga_result`、`ga_send`、`ga_stop`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_acp_tools.py
import asyncio
import json
import sys

import pytest

from app.acp.config import load_acp_settings
from app.acp.manager import AcpSessionManager, AcpUnavailableError
from app.acp.tools import build_acp_tools
from app.subagent.context import AgentContext
from app.subagent.events import EventBuffer


class FakeManager:
    def __init__(self):
        self.calls = []

    async def spawn(self, **kw):
        self.calls.append(("spawn", kw))
        return {"agent_id": "ga_x", "status": "running"}

    async def wait(self, agent_id, **kw):
        self.calls.append(("wait", agent_id, kw))
        return {"agents": [], "timed_out": False}

    async def result(self, agent_id, **kw):
        self.calls.append(("result", agent_id, kw))
        return {"agent_id": agent_id, "status": "completed", "result": "ok"}

    async def send(self, agent_id, message):
        self.calls.append(("send", agent_id, message))
        return {"agent_id": agent_id, "status": "running"}

    async def stop(self, agent_id):
        self.calls.append(("stop", agent_id))
        return {"stopped": [agent_id] if agent_id else []}


class BrokenManager(FakeManager):
    async def spawn(self, **kw):
        raise RuntimeError("boom")


def run(coro):
    return asyncio.run(coro)


def tools_of(mgr):
    return {t.name: t for t in build_acp_tools(mgr)}


def test_exact_tool_names():
    assert set(tools_of(FakeManager())) == {
        "ga_spawn", "ga_wait", "ga_result", "ga_send", "ga_stop"}


def test_spawn_passes_arguments():
    mgr = FakeManager()
    out = run(tools_of(mgr)["ga_spawn"].call(
        {"task": "fix it", "title": "T", "mode": "read_only", "timeout_s": 30}))
    assert "ga_x" in out
    assert mgr.calls[0][1] == {"task": "fix it", "title": "T",
                               "mode": "read_only", "timeout_s": 30.0}


def test_wait_defaults():
    mgr = FakeManager()
    run(tools_of(mgr)["ga_wait"].call({}))
    assert mgr.calls[0] == ("wait", None, {"timeout_s": 60.0, "since_seq": 0})


def test_result_close_flag():
    mgr = FakeManager()
    run(tools_of(mgr)["ga_result"].call({"agent_id": "ga_x", "close": True, "full": True}))
    assert mgr.calls[0] == ("result", "ga_x",
                            {"max_chars": 20000, "full": True, "close": True})


def test_tool_errors_become_readable_text():
    out = run(tools_of(BrokenManager())["ga_spawn"].call({"task": "x"}))
    assert out.startswith("[工具执行错误]")
    assert "boom" in out


def test_preflight_fails_fast_for_missing_module(tmp_path):
    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    cmd = json.dumps([sys.executable, "-m", "definitely_not_a_module_xyz"])
    settings = load_acp_settings({"INNER_ACP_GA_COMMAND": cmd})
    mgr = AcpSessionManager(ctx=ctx, buffer=EventBuffer(), settings=settings)
    with pytest.raises(AcpUnavailableError) as exc:
        mgr.preflight()
    assert "definitely_not_a_module_xyz" in str(exc.value)
```

> 预检是同步方法；Python 启动 + import 失败在 15 秒超时内返回。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_tools.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.acp.tools'`

- [ ] **Step 3: 实现 `tools.py` 与 `preflight()`**

- `preflight()`：
  - 若 `settings.command` 不含 `-m`（自定义命令）→ 返回 `None`；
  - 否则取 `module = command[command.index("-m") + 1]`，执行
    `[command[0], "-c", f"import {module} as b; print(b.__file__)"]`，
    `capture_output=True, text=True, timeout=15`；
  - 非 0 或 stdout 为空 → 抛 `AcpUnavailableError(f"GenericAgent ACP bridge 不可用: {stderr[-800:]}")`；
  - 成功返回路径，并对文件算 SHA256 写入 logger；结果按 `tuple(command)` 缓存。
- `spawn()` 第一步调用 `self.preflight()`。
- `build_acp_tools(manager)` 返回 5 个 `Tool`，schema 与 spec §7.2 一致；每个 `call`：
  - 参数按类型转换（`float(args.get("timeout_s", 60) or 60)`）；
  - 捕获 `Exception` 返回 `f"[工具执行错误] {type(e).__name__}: {e}"`；
  - 结果 `json.dumps(..., ensure_ascii=False)`。
- 工具描述直接使用 spec §7.3 的四行文案 + `ga_send` / `ga_stop` 各一行。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_tools.py -q`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/acp/tools.py app/acp/manager.py tests/test_acp_tools.py
git commit -m "feat(acp): add preflight and the five ga_* tools"
```

---

## Task 8: 挂载到本轮 runtime

**Files:**
- Modify: `backend/app/subagent/session.py:38-49`、`:86-90`、`:150-160`
- Test: `backend/tests/test_acp_mount.py`

**Interfaces:**
- Consumes: `load_acp_settings`（Task 1）、`AcpSessionManager` / `build_acp_tools`（Tasks 6–7）
- Produces: `TurnRuntime.acp: AcpSessionManager | None`；root registry 在配置启用时含 5 个 `ga_*` 工具。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_acp_mount.py
import asyncio
import sys

from app.subagent.context import AgentContext
from app.tools import Tool, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def mk(n):
    async def call(args):
        return "ok"
    return Tool(n, "", {"type": "object", "properties": {}}, call)


class FakeModel:
    model_name = "fake"
    client = object()


class FakeBaseLoop:
    system_prompt = "SYS"


class FakeBase:
    model = FakeModel()
    loop = FakeBaseLoop()
    store = object()
    registry = ToolRegistry([mk("file_read")])


def build(tmp_path, monkeypatch):
    from app import workspace
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    from app.subagent import store as sub_store
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "s.db")
    run(sub_store.init_tables())
    from app.subagent.session import build_turn_runtime
    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    return run(build_turn_runtime(FakeBase(), ctx))


def test_no_ga_tools_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("INNER_ACP_GA_COMMAND", raising=False)
    monkeypatch.delenv("INNER_ACP_GA_PYTHON", raising=False)
    turn = build(tmp_path, monkeypatch)
    names = set(turn.loop.registry.names())
    assert turn.acp is None
    assert not any(n.startswith("ga_") for n in names)


def test_ga_tools_injected_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("INNER_ACP_GA_PYTHON", sys.executable)
    turn = build(tmp_path, monkeypatch)
    names = set(turn.loop.registry.names())
    assert turn.acp is not None
    for n in ("ga_spawn", "ga_wait", "ga_result", "ga_send", "ga_stop"):
        assert n in names
    # 现有能力不受影响
    assert "file_read" in names
    assert "spawn_agent" in names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_mount.py -q`
Expected: FAIL，`AttributeError: 'TurnRuntime' object has no attribute 'acp'`

- [ ] **Step 3: 实现**

- `TurnRuntime` 增加 `acp: AcpSessionManager | None = None`。
- 在 `host` / `build_subagent_tools` 之后：

```python
    acp = None
    acp_settings = load_acp_settings()
    if acp_settings.enabled:
        from ..acp.manager import AcpSessionManager
        from ..acp.tools import build_acp_tools
        acp = AcpSessionManager(ctx=team_ctx, buffer=buffer, settings=acp_settings)
        for t in build_acp_tools(acp):
            merged.add(t)
```

- 构造 `TurnRuntime(...)` 时传 `acp=acp`。
- **失败降级**：`AcpSessionManager` 构造或工具注入抛异常时，记 warning，`acp=None`，不影响本轮其他工具。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_mount.py tests/test_subagent_chat.py -q`
Expected: 新增 2 passed，原有全部通过

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/subagent/session.py tests/test_acp_mount.py
git commit -m "feat(acp): inject ga_* tools into the root turn runtime"
```

---

## Task 9: RuntimeHub 注册表

**Files:**
- Modify: `backend/app/runtime.py`（`RuntimeHub.__init__` / 新增 4 个方法 / `shutdown`）
- Test: `backend/tests/test_acp_stop.py`（本任务部分）

**Interfaces:**
- Produces:
  - `register_acp_manager(user_id, session_id, generation_id, manager) -> None`
  - `unregister_acp_manager(user_id, session_id, generation_id) -> None`
  - `get_acp_manager(user_id, session_id, generation_id) -> object | None`
  - `async stop_acp_generation(user_id, session_id, generation_id) -> int`
- 键与现有 `_subagent_hosts` 相同：`(user_id, session_id, generation_id)`。
- `shutdown()` 里对每个 manager `await manager.close_all()` 并清表。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_acp_stop.py
import asyncio

from app.runtime import RuntimeHub


def run(coro):
    return asyncio.run(coro)


class FakeAcp:
    def __init__(self):
        self.stopped = 0
        self.closed = 0

    async def stop_generation(self):
        self.stopped += 1
        return 2

    async def close_all(self):
        self.closed += 1


def test_register_get_unregister():
    hub = RuntimeHub()
    m = FakeAcp()
    hub.register_acp_manager("u", "s", "g", m)
    assert hub.get_acp_manager("u", "s", "g") is m
    hub.unregister_acp_manager("u", "s", "g")
    assert hub.get_acp_manager("u", "s", "g") is None


def test_stop_acp_generation_delegates():
    async def scenario():
        hub = RuntimeHub()
        m = FakeAcp()
        hub.register_acp_manager("u", "s", "g", m)
        n = await hub.stop_acp_generation("u", "s", "g")
        return n, m.stopped

    assert run(scenario()) == (2, 1)


def test_stop_unknown_generation_is_noop():
    async def scenario():
        return await RuntimeHub().stop_acp_generation("u", "s", "g")

    assert run(scenario()) == 0


def test_shutdown_closes_acp_managers():
    async def scenario():
        hub = RuntimeHub()
        m = FakeAcp()
        hub.register_acp_manager("u", "s", "g", m)
        await hub.shutdown()
        return m.closed, hub.get_acp_manager("u", "s", "g")

    assert run(scenario()) == (1, None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_stop.py -q`
Expected: FAIL，`AttributeError: 'RuntimeHub' object has no attribute 'register_acp_manager'`

- [ ] **Step 3: 实现**

照抄 `_subagent_hosts` 的三个方法结构（`backend/app/runtime.py:29-52`），另加：

```python
    async def stop_acp_generation(self, user_id, session_id, generation_id) -> int:
        mgr = self._acp_managers.get((user_id, session_id, generation_id))
        if mgr is None:
            return 0
        try:
            return await mgr.stop_generation()
        except Exception:
            return 0
```

`shutdown()` 在清空 subagent hosts 后：

```python
        for mgr in list(self._acp_managers.values()):
            try:
                await mgr.close_all()
            except Exception:
                pass
        self._acp_managers.clear()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_stop.py -q`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/runtime.py tests/test_acp_stop.py
git commit -m "feat(runtime): register per-generation ACP managers"
```

---

## Task 10: chat 集成（事件 / 停止 / 清理）

**Files:**
- Modify: `backend/app/routers/chat.py:29-118`、`:155-212`、`:404`、`:611-629`
- Test: `backend/tests/test_acp_stop.py`（追加）

**Interfaces:**
- `_merged_events(main_gen, buffer, host, stop_event=None, extra=None)`：`extra` 需有
  `has_active() -> bool` 与 `async stop_all() -> int`。
- `_cleanup_turn` 在同步段调用 `turn.acp.stop_all_now()`；`_settle_turn` 调用 `await turn.acp.close_all()`。
- `/stop` 顺序：`hub.stop_subagent_generation` → `hub.stop_acp_generation` → `stop_code_runs`。

- [ ] **Step 1: 追加失败测试**

```python
# backend/tests/test_acp_stop.py（追加）
import pytest


def test_merged_events_waits_for_acp_extra():
    from app.routers.chat import _merged_events
    from app.subagent.events import EventBuffer

    class Extra:
        active = True

        def has_active(self):
            return self.active

        async def stop_all(self):
            self.active = False

    async def main_gen():
        yield {"type": "done", "status": "complete"}

    async def scenario():
        buf = EventBuffer()
        extra = Extra()

        async def child():
            await asyncio.sleep(0.05)
            buf.put({"type": "subagent_end", "agent_id": "ga_x#1",
                     "status": "completed", "turns": 0})
            extra.active = False

        task = asyncio.create_task(child())
        out = []
        async for ev in _merged_events(main_gen(), buf, object(), extra=extra):
            out.append(ev["type"])
        await task
        return out

    assert "subagent_end" in run(scenario())


def test_merged_events_stops_acp_extra_on_drain_timeout(monkeypatch):
    from app.routers.chat import _merged_events
    from app.subagent.events import EventBuffer

    monkeypatch.setenv("TEAM_DRAIN_TIMEOUT_S", "0.1")

    class Extra:
        def __init__(self):
            self.stops = 0

        def has_active(self):
            return True

        async def stop_all(self):
            self.stops += 1

    async def main_gen():
        yield {"type": "done", "status": "complete"}

    async def scenario():
        extra = Extra()
        async for _ in _merged_events(main_gen(), EventBuffer(), object(), extra=extra):
            pass
        return extra.stops

    assert run(scenario()) >= 1


def test_stop_endpoint_orders_graceful_acp_before_hard_kill(monkeypatch):
    from app import db
    from app.routers import chat
    from app.schemas import StopIn

    order = []

    async def fake_get_session(sid, uid):
        return {"id": sid, "user_id": uid}

    class FakeHub:
        async def stop_subagent_generation(self, sid, gid):
            order.append("subagents")
            return 1

        async def stop_acp_generation(self, uid, sid, gid):
            order.append("acp")
            return 1

    async def fake_stop_code_runs(uid, sid, gid):
        order.append("code_runs")

    monkeypatch.setattr(chat, "get_hub", lambda: FakeHub())
    monkeypatch.setattr(chat, "request_stop", lambda sid, gid: 1)
    monkeypatch.setattr(chat, "stop_code_runs", fake_stop_code_runs)
    monkeypatch.setattr(db, "get_session", fake_get_session)

    run(chat.stop_generation("s1", StopIn(generation_id="g1"), {"id": "u1"}))
    assert order == ["subagents", "acp", "code_runs"]


def test_cleanup_calls_acp_stop_all_now_before_awaits():
    from app.routers import chat

    calls = []

    class Acp:
        def stop_all_now(self):
            calls.append("acp_sync")

        async def close_all(self):
            calls.append("acp_close")

    class Host:
        def stop_all_now(self):
            calls.append("host_sync")

        async def close_all(self):
            calls.append("host_close")

    class Team:
        async def events_since(self, seq):
            return []

        async def set_status(self, status):
            return None

    class Turn:
        host = Host()
        team = None
        acp = Acp()

    class Hub:
        def unregister_subagent_host(self, *a):
            calls.append("unregister")

    async def scenario():
        await chat._cleanup_turn(Turn(), Hub(), "u", "s", "g", "", "stopped")

    run(scenario())
    assert calls.index("acp_sync") < calls.index("acp_close")
    assert calls.index("acp_sync") < calls.index("host_close")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_acp_stop.py -q`
Expected: 前 4 个通过，新 4 个失败（`_merged_events` 不接受 `extra`、stop 顺序缺少 acp、cleanup 未调用）

- [ ] **Step 3: 实现**

1. `_merged_events` 签名加 `extra=None`；内部：
   ```python
   def extra_active() -> bool:
       fn = getattr(extra, "has_active", None)
       try:
           return bool(fn()) if callable(fn) else False
       except Exception:
           return False
   ```
   `has_active()` 总判断改为 `host_has_active() or extra_active()`。
   drain 超时分支：调用完 `host.stop_all()` 后，若 `extra` 有 `stop_all` 则 `await extra.stop_all()`。
2. 调用点（`:404` 附近）传入 `extra=turn.acp`。
3. `_cleanup_turn` 同步段：`host.stop_all_now()` 之后加
   ```python
   acp_stop_now = getattr(getattr(turn, "acp", None), "stop_all_now", None)
   if callable(acp_stop_now):
       try:
           acp_stop_now()
       except BaseException:
           logger.warning("stop ACP sessions failed", exc_info=True)
   ```
4. `_cleanup_turn` 在 `await turn.host.close_all()` 之前或之后加 `await turn.acp.close_all()`（放在 host 之后、team 之前）。
5. `_settle_turn` 开头加 `await turn.acp.close_all()`（`getattr` 保护）。
6. `/stop` 端点：`hub.stop_subagent_generation` 之后插入
   ```python
   try:
       await hub.stop_acp_generation(user["id"], session_id, generation_id)
   except Exception:
       pass
   ```
   再执行 `stop_code_runs`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_acp_stop.py tests/test_subagent_chat.py -q`
Expected: 8 + 原有全部通过

- [ ] **Step 5: 提交**

```bash
cd backend && git add app/routers/chat.py tests/test_acp_stop.py
git commit -m "feat(chat): merge ACP events and cascade stop before hard kill"
```

---

## Task 11: 前端标签与历史回放

**Files:**
- Modify: `frontend/src/components/SubagentCard.tsx:5-7`
- Verify: `frontend/src/App.tsx:533-561`（不改）

**Interfaces:**
- `ROLE_LABEL` 增加 `ga: 'GA 外援'`；block 类型仍为 `subagent`，历史回放零改动。

- [ ] **Step 1: 改一行**

```tsx
const ROLE_LABEL: Record<string, string> = {
  general: '通用',
  ga: 'GA 外援',
}
```

- [ ] **Step 2: 构建验证**

Run: `cd frontend && npm run build`
Expected: 构建成功，无 TypeScript 错误

- [ ] **Step 3: 提交**

```bash
cd frontend && git add src/components/SubagentCard.tsx
git commit -m "feat(ui): label GenericAgent subagent cards"
```

---

## Task 12: 真实 GA E2E 与文档（opt-in）

**前置条件：** 本机已安装 GenericAgent，且 `INNER_ACP_GA_PYTHON` 指向能 import
`frontends.acp.genericagent_acp_bridge` 的解释器。**不满足则跳过本任务，前 11 个任务仍可交付。**

- [ ] **Step 1: 配置预检**

```bash
cd backend
export INNER_ACP_GA_PYTHON="C:/anaconda3/python.exe"
python -c "import frontends.acp.genericagent_acp_bridge as b; print(b.__file__)"
```

Expected: 打印 `.../site-packages/frontends/acp/genericagent_acp_bridge.py`；
把该文件 SHA256 写入实施记录。

- [ ] **Step 2: 哨兵 E2E（真 GA 写文件）**

启动后端，从一个已登录的 Web 会话发：

```text
请用 ga_spawn 让 GenericAgent 在当前 workspace 创建 acp_ga_e2e.txt，
文件内容严格为 ACP_GA_OK_<本次随机8位>，完成后用 ga_result 告诉我结果。
```

验收：

- 消息流出现 `GA 外援` 卡片，含文本与工具步骤；
- `acp_ga_e2e.txt` 存在且内容含该哨兵（用 `file_read` 复核）；
- `ga_result` 返回的文本非空；
- 数据库 `blocks` 里该 block `type == "subagent"`、`agent_id` 含 `#1`。

- [ ] **Step 2b: 多步任务 E2E（读 → 改 → 跑）**

在同一个 workspace 里放一个带断言的小脚本，再发：

```text
请用 ga_spawn 让 GenericAgent 读取 workspace 里的 calc.py，
把 add 函数的 bug 修好，然后运行 python calc.py 验证，最后用 ga_result 回报。
```

验收：`ga_result` 的 `result` 提到运行成功；`ga_spawn` 卡片里至少出现 3 个工具步骤
（读 / 改 / 执行）；`calc.py` 的内容确实被修改。

- [ ] **Step 3: 长任务与停止**

发一个要求 GA `sleep 60` 的任务，在卡片 running 时点停止。验收：

- 3 秒内 `tasklist | findstr genericagent_acp_bridge` 无输出；
- 卡片状态 `stopped`；
- 主对话可以立即继续发送。

- [ ] **Step 4: 写实施记录**

新建 `docs/20261109-ACP-client驱动GenericAgent实施记录.md`，至少包含：
GA bridge 路径与 SHA256、`agentInfo.version`、三个 E2E 的实际输出、`tasklist` 复核结果、
未覆盖项（GA 内部 subagent 事件、跨轮续跑）。

- [ ] **Step 5: 更新 README / AGENTS**

- `README.md` 能力清单加「外部 GenericAgent（ACP）」；
- `AGENTS.md` §0.1 增加 P5 完成条目，指向实施记录；
- **未实际跑通 E2E 时不要写“已完成”**，只写“已实现，E2E 待验”。

- [ ] **Step 6: 提交**

```bash
git add docs/20261109-ACP-client驱动GenericAgent实施记录.md README.md AGENTS.md
git commit -m "docs: record ACP-GenericAgent E2E and capability entry"
```

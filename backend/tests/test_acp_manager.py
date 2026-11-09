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
        # 子进程环境走白名单；FAKE_GA_MODE 只有显式允许才会传给 fixture。
        "INNER_ACP_ENV_ALLOW": "FAKE_GA_MODE,FAKE_GA_LOG",
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
    assert r["stop_reason"] == "end_turn"
    assert active is False


def test_result_close_keeps_completed_status(monkeypatch):
    """ga_result(close=true) 不能把正常完成的会话降级成 stopped（终态只进一次）。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, _ = make()
        try:
            aid = (await mgr.spawn(task="say hi"))["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            return await mgr.result(aid, close=True)
        finally:
            await mgr.close_all()

    r = run(scenario())
    assert r["closed"] is True
    assert r["status"] == "completed"
    assert r["result"] == "hello world"


def test_ga_stop_on_completed_session_keeps_completed(monkeypatch):
    """已完成会话再 ga_stop 仍是 completed：stop() 对终态必须是 no-op。"""
    monkeypatch.setenv("FAKE_GA_MODE", "chunks")

    async def scenario():
        mgr, _ = make()
        try:
            aid = (await mgr.spawn(task="say hi"))["agent_id"]
            await mgr.wait(aid, timeout_s=10)
            before = await mgr.result(aid)
            await mgr.stop(aid)
            after = await mgr.result(aid)
            return before, after
        finally:
            await mgr.close_all()

    before, after = run(scenario())
    assert before["status"] == "completed"
    assert after["status"] == "completed"
    assert after["result"] == before["result"] == "hello world"


def test_timeout_reports_stop_reason(monkeypatch):
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="slow", timeout_s=0.3)
            await mgr.wait(out["agent_id"], timeout_s=10)
            return await mgr.result(out["agent_id"])
        finally:
            await mgr.close_all()

    r = run(scenario())
    assert r["status"] == "stopped"
    assert r["stop_reason"] == "timeout"


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
            session = mgr._sessions[out["agent_id"]]
            handle = session.process_handle
            mgr.stop_all_now()
            for _ in range(50):
                if handle.returncode is not None:
                    return True, session.snapshot()["status"]
                await asyncio.sleep(0.1)
            return False, session.snapshot()["status"]
        finally:
            await mgr.close_all()

    killed, status = run(scenario())
    assert killed is True
    assert status == "stopped"


def test_stop_all_now_shares_one_reap_budget(monkeypatch):
    """同步清理路径不能每条会话各阻塞 2s：kill 只发出、收尸共享一个短窗口。"""
    import app.acp.manager as manager_mod

    calls = []

    def slow_kill(handle, wait_s=None):
        calls.append(wait_s)
        # 旧实现根本不传 wait_s（= 每条会话固定阻塞 2s 收尸）。
        time.sleep(2.0 if wait_s is None else wait_s)
        handle.exited = True

    monkeypatch.setattr(manager_mod, "kill_tree_now", slow_kill)

    async def scenario():
        mgr, _ = make()
        handles = [_FakeHandle(), _FakeHandle()]
        try:
            _install_fakes(
                mgr,
                [
                    _FakeGaSession("ga_a", handle=handles[0]),
                    _FakeGaSession("ga_b", handle=handles[1]),
                ],
            )
            t0 = time.monotonic()
            mgr.stop_all_now()
            return time.monotonic() - t0, handles
        finally:
            await mgr.close_all()

    elapsed, handles = run(scenario())
    # 旧实现两条会话串行 4s；新实现只发 kill（wait_s=0）再共享短窗口。
    assert elapsed < 1.0
    assert calls == [0.0, 0.0]
    assert all(handle.exited for handle in handles)


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


def test_spawn_failure_includes_stderr_tail():
    """spec §9.5 / §11.3：spawn 失败的错误文本要带 stderr 最后几行。"""
    marker = "GA_SPAWN_STDERR_MARKER_7f3c"
    command = json.dumps([
        sys.executable,
        "-c",
        f"import sys; sys.stderr.write({marker!r} + '\\n'); sys.exit(2)",
    ])

    async def scenario():
        mgr, _ = make(INNER_ACP_GA_COMMAND=command)
        try:
            with pytest.raises(RuntimeError) as excinfo:
                await mgr.spawn(task="boom")
            return str(excinfo.value)
        finally:
            await mgr.close_all()

    message = run(scenario())
    assert "启动 GenericAgent 失败" in message
    assert marker in message


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


# ---------------------------------------------------------------- 停止/预览
# 这些用例不启动真进程：只把 _entries/_sessions 换成假的会话对象，
# 用来确定性地观察「并发」与「是否把阻塞杀放进线程」。


class _FakeHandle:
    """只有 poll() 的假进程句柄：被杀之后才算退出。"""

    def __init__(self):
        self.exited = False

    def poll(self):
        return 0 if self.exited else None


class _FakeGaSession:
    """只实现 stop/close 路径用到的最小接口。"""

    def __init__(self, agent_id, *, stop_delay=0.0, handle=None):
        self.agent_id = agent_id
        self.status = "running"
        self.process_handle = handle if handle is not None else object()
        self.stop_delay = stop_delay
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)
        self.status = "stopped"

    def mark_stopped(self):
        self.status = "stopped"

    async def close(self):
        pass


def _install_fakes(mgr, sessions, *, tasks=None):
    import app.acp.manager as manager_mod
    from app.process_registry import ProcessLease

    mgr._entries.clear()
    mgr._sessions.clear()
    for session in sessions:
        entry = manager_mod._Entry(
            session=session,
            lease=ProcessLease(
                mgr.ctx.user_id,
                mgr.ctx.session_id,
                mgr.ctx.generation_id,
                session.agent_id,
            ),
            task=(tasks or {}).get(session.agent_id),
        )
        mgr._entries[session.agent_id] = entry
        mgr._sessions[session.agent_id] = session


def test_stop_all_now_before_prompt_task_runs_stays_stopped(monkeypatch):
    """spawn 的 create_task 与 stop_all_now 之间没有 await：任务晚到时
    不能再发卡片、也不能把 stopped 翻成 errored。"""
    monkeypatch.setenv("FAKE_GA_MODE", "hang")

    async def scenario():
        mgr, buf = make()
        try:
            out = await mgr.spawn(task="long")
            session = mgr._sessions[out["agent_id"]]
            mgr.stop_all_now()
            await mgr.wait(out["agent_id"], timeout_s=5)
            starts = [e for e in buf.events if e["type"] == "subagent_start"]
            return session.snapshot()["status"], starts
        finally:
            await mgr.close_all()

    status, starts = run(scenario())
    assert status == "stopped"
    assert starts == []


def test_wait_preview_shows_text_while_running(monkeypatch):
    """进行中的会话也要能 preview 到中间文本（result/full_text 此时还没拼）。"""
    monkeypatch.setenv("FAKE_GA_MODE", "stream_hang")

    async def scenario():
        mgr, _ = make()
        try:
            out = await mgr.spawn(task="long")
            r = await mgr.wait(out["agent_id"], timeout_s=1.0)
            await mgr.stop(out["agent_id"])
            return r
        finally:
            await mgr.close_all()

    r = run(scenario())
    row = r["agents"][0]
    assert r["timed_out"] is True
    assert row["status"] == "running"
    assert row["result_preview"] == "working..."


def test_stop_generation_stops_sessions_concurrently(monkeypatch):
    async def scenario():
        mgr, _ = make()
        try:
            _install_fakes(
                mgr,
                [
                    _FakeGaSession("ga_a", stop_delay=0.6),
                    _FakeGaSession("ga_b", stop_delay=0.6),
                ],
            )
            t0 = time.monotonic()
            count = await mgr.stop_generation()
            return count, time.monotonic() - t0
        finally:
            await mgr.close_all()

    count, elapsed = run(scenario())
    assert count == 2
    # 串行约 1.2s；并发约 0.6s，留 0.4s 余量防抖动
    assert elapsed < 1.0


def test_async_stop_runs_killer_off_the_event_loop(monkeypatch):
    import app.acp.manager as manager_mod

    monkeypatch.setattr(manager_mod, "_STOP_GRACE_S", 0.01)
    monkeypatch.setattr(manager_mod, "_KILL_SETTLE_S", 0.01)
    killed = []

    def slow_kill(handle):
        killed.append(handle)
        time.sleep(0.6)  # 同步阻塞：只有放进线程才不会冻住 SSE

    monkeypatch.setattr(manager_mod, "kill_tree_now", slow_kill)

    async def scenario():
        mgr, _ = make()
        never = asyncio.create_task(asyncio.sleep(30))
        try:
            _install_fakes(mgr, [_FakeGaSession("ga_slow")], tasks={"ga_slow": never})
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            tick = asyncio.create_task(ticker())
            try:
                count = await mgr.stop_generation()
            finally:
                tick.cancel()
                never.cancel()
                await asyncio.gather(tick, never, return_exceptions=True)
            return count, ticks, len(killed)
        finally:
            await mgr.close_all()

    count, ticks, kills = run(scenario())
    assert count == 1
    assert kills == 1
    # 0.6s 的同步杀若占住事件循环，ticks 几乎不会增长
    assert ticks >= 10

def test_preflight_tolerates_bridge_that_redirects_stdout(tmp_path, monkeypatch):
    """真实 bridge 在 import 时把 fd1 / sys.stdout 重定向到 stderr（bridge:14-45）。

    预检只解析模块路径、不执行目标模块，所以这种 bridge 必须能通过预检；
    否则一个完全健康的安装会被判成不可用，``ga_*`` 工具永远不注入。
    """
    from app.acp.manager import reset_preflight_cache

    name = "ga_bridge_stdout_redirect_stub"
    stub = tmp_path / f"{name}.py"
    stub.write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "# 复刻真实 bridge：import 时把 fd1 / sys.stdout 重定向到 stderr。\n"
        "sys.stdout = sys.stderr\n"
        "os.dup2(sys.stderr.fileno(), 1)\n"
        "print('bridge imported')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    # 探测子进程只继承 build_child_env 的白名单，必须显式允许 PYTHONPATH 才看得到 stub。
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    reset_preflight_cache()

    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")
    settings = load_acp_settings({
        "INNER_ACP_GA_COMMAND": json.dumps([sys.executable, "-m", name]),
        "INNER_ACP_ENV_ALLOW": "PYTHONPATH",
    })
    mgr = AcpSessionManager(ctx=ctx, buffer=RecordingBuffer(), settings=settings)
    try:
        path = mgr.preflight()
    finally:
        reset_preflight_cache()

    assert path is not None
    assert Path(path).resolve() == stub.resolve()

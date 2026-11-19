"""接线测试：预热**必须真的被复用**，而不是只记账。

## 为什么单独一个文件

这是上一轮的真实失误：`_DAEMON_SCRIPT` 写好了、池里也加了 prewarm 参数，
但三处接线全断：

1. `get_executor()` **每次调用都新建实例** → 预热起在实例 A 上，
   `exec` 时拿的是实例 B，永远复不上（比"没接 prewarm"更隐蔽）
2. `_get_pool()` 没把 `prewarm` 传进去 → 池压根不预热
3. 槽销毁/淘汰/空闲回收时**没人停 daemon** → 进程泄漏，直到容器 OOM

`test_bwrap_daemon.py` 测的是执行器**内部**协议对不对；
本文件测的是**服务层有没有把它接上**——两者都绿才算真的能用。

## 断言口径

不断言"调用了某个函数"，而是断言**同一个对象跨调用存活**：
预热与执行必须落在同一个执行器实例上，否则 daemon 无处可寻。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _FakeExecutor:
    """记录 start / exec / destroy 的调用与**实例身份**。"""

    def __init__(self, start_result: bool | None = True) -> None:
        self.start_result = start_result
        self.started: list[str] = []
        self.destroyed: list[str] = []
        self.execs: list[str] = []

    async def start(self, user_id):
        self.started.append(user_id)
        return self.start_result

    async def exec(self, user_id, code, *, code_type="python", timeout_s=120.0, cwd=""):
        from sandbox_service.executor.base import ExecResult

        self.execs.append(user_id)
        return ExecResult(0, "ok", "", 1, False)

    async def destroy(self, user_id):
        self.destroyed.append(user_id)

    async def health(self):
        return True, "ok"


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    """干净的 main 模块状态：池/执行器全局都重置。"""
    from sandbox_service import main as svc_main

    monkeypatch.setenv("HLZ_SANDBOX_WORKSPACES_DIR", str(tmp_path))
    from sandbox_service import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "DEFAULT_WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(svc_main, "_pool", None)
    monkeypatch.setattr(svc_main, "_executor", None)
    return svc_main


# ---------------------------------------------------------------- 1. 执行器单例


def test_executor_is_singleton_across_calls(svc):
    """★ 核心：两次 `_get_executor()` 必须是**同一个对象**。

    daemon 存在执行器实例的 `_daemons` 字典里。如果每次调用都新建实例，
    预热起的 daemon 立刻失去引用 → 变成孤儿进程，且 exec 复不上（走一次性）。
    """
    a = svc._get_executor()
    b = svc._get_executor()
    assert a is b, (
        "get_executor() 每次返回新实例——预热的 daemon 存在旧实例里，"
        "exec 时拿到的是新实例，永远复不上"
    )


def test_reset_executor_clears_singleton(svc):
    """测试专用重置口存在且有效（生产不使用）。"""
    a = svc._get_executor()
    svc._reset_executor()
    b = svc._get_executor()
    assert a is not b


# ---------------------------------------------------------------- 2. 池真的预热


def test_pool_gets_prewarm_callback(svc, monkeypatch):
    """`_get_pool()` 必须把预热回调交给池——否则池只记账不预热。"""
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)

    pool = svc._get_pool()
    assert pool._prewarm is not None, "池没有预热回调：建槽时不会起 daemon"


def test_acquire_slot_calls_executor_start(svc, monkeypatch, tmp_path):
    """建槽时必须真的调 `executor.start(user_id)`。"""
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    (tmp_path / "u1").mkdir()

    pool = svc._get_pool()
    asyncio.run(pool.acquire("u1"))
    assert fake.started == ["u1"], f"建槽没预热，start 调用为 {fake.started}"


def test_prewarm_and_exec_share_one_instance(svc, monkeypatch, tmp_path):
    """★ 端到端接线：预热与执行必须落在**同一个执行器实例**上。

    这是「复用」的定义。两个实例各自 `start` 一次也全是绿的，
    但第二条 exec 依然走一次性模式——本仓库上一轮就是这样假验收的。
    """
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    (tmp_path / "u1").mkdir()

    async def go():
        pool = svc._get_pool()
        slot = await pool.acquire("u1")
        ex = svc._get_executor()
        await ex.exec("u1", "print(1)")
        await pool.release("u1")
        return slot, ex

    slot, ex = asyncio.run(go())
    assert ex is fake
    assert fake.started == ["u1"]
    assert fake.execs == ["u1"]


# ---------------------------------------------------------------- 3. 销毁要停 daemon


def test_destroy_slot_stops_daemon(svc, monkeypatch, tmp_path):
    """显式销毁槽必须停掉 daemon，否则进程泄漏。"""
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    (tmp_path / "u1").mkdir()

    async def go():
        pool = svc._get_pool()
        await pool.acquire("u1")
        await pool.release("u1")
        await pool.destroy("u1")

    asyncio.run(go())
    assert fake.destroyed == ["u1"], f"销毁槽没停 daemon：{fake.destroyed}"


def test_evict_stops_daemon(svc, monkeypatch, tmp_path):
    """淘汰槽必须停掉 daemon（否则 evict 越多、孤儿进程越多）。"""
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    for uid in ("u1", "u2"):
        (tmp_path / uid).mkdir()

    async def go():
        pool = svc._get_pool()
        await pool.acquire("u1")
        await pool.release("u1")
        await pool.acquire("u2")      # max_slots=3 会直接建；手动逼 evict

    asyncio.run(go())
    pool = svc._get_pool()

    async def evict():
        await pool._evict(pool._slots["u1"])

    asyncio.run(evict())
    assert "u1" in fake.destroyed


def test_reap_idle_stops_daemon(svc, monkeypatch, tmp_path):
    """空闲 TTL 回收槽必须停掉 daemon（这是最主要的长期泄漏源）。"""
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    (tmp_path / "u1").mkdir()

    async def go():
        pool = svc._get_pool()
        pool.idle_ttl_s = 0        # 立刻到期
        await pool.acquire("u1")
        await pool.release("u1")
        return await pool.reap_idle()

    assert asyncio.run(go()) == 1
    assert fake.destroyed == ["u1"], "空闲回收没停 daemon"


# ---------------------------------------------------------------- 4. 降级语义


def test_prewarm_none_counts_as_success(svc, monkeypatch, tmp_path):
    """`start()` 返回 None 的执行器（inproc）不能被当成预热失败。

    `InprocExecutor.start` 返回 None（它没有 daemon 可起）。
    `None` 是 falsy，若直接 `if ok:` 判定就会每次建槽都打一条
    「预热失败，退化到一次性模式」的 WARNING——日志全是噪音，
    真正的问题反而看不见。
    """
    fake = _FakeExecutor(start_result=None)
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    (tmp_path / "u1").mkdir()

    async def go():
        pool = svc._get_pool()
        slot = await pool.acquire("u1")
        return slot

    slot = asyncio.run(go())
    assert slot.parent_proc == {"prewarm_ok": True}


def test_prewarm_false_is_not_fatal(svc, monkeypatch, tmp_path):
    """预热失败**不能**让建槽失败——退化到一次性模式仍要能跑。"""
    fake = _FakeExecutor(start_result=False)
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    (tmp_path / "u1").mkdir()

    async def go():
        pool = svc._get_pool()
        slot = await pool.acquire("u1")
        await pool.release("u1")
        return slot

    slot = asyncio.run(go())
    assert slot.parent_proc == {"prewarm_ok": False}
    assert svc._get_pool().users() == {"u1"}


def test_prewarm_exception_is_not_fatal(svc, monkeypatch, tmp_path):
    """预热抛异常同样不能让建槽失败。"""
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    (tmp_path / "u1").mkdir()

    async def boom(user_id):
        raise RuntimeError("bwrap 没装")

    async def go():
        pool = svc._get_pool()
        pool._prewarm = boom
        return await pool.acquire("u1")

    slot = asyncio.run(go())
    assert slot.parent_proc == {"prewarm_ok": False}

# ---------------------------------------------------------------- 5. 空闲回收循环


def test_reap_loop_is_started_by_lifespan(svc, monkeypatch, tmp_path):
    """★ 生命周期里必须起空闲回收协程。

    `reap_idle()` 写得再对，**没人调就等于没有**。上一轮就是这样：
    `IDLE_TTL_S` 配了、函数实现了，服务里没有任何地方调它 ——
    槽只涨不落，到上限就永远排队（因为「TTL 回收腾出名额」是 queue 的出路之一）。

    ## 断言口径：认协程身份，不认「有没有任务」

    第一版写成「lifespan 期间创建过 at least 1 个 task」——
    反向确认时把起回收换成 `create_task(asyncio.sleep(0))`，测试**照样绿**。
    所以必须看协程对象**是不是 `_reap_loop`**：

        coro.cr_code.co_name == "_reap_loop"

    这类"断言太宽 = 反向确认也拦不住"的假绿，本仓库已踩过多次。
    """
    from fastapi.testclient import TestClient

    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    names: list[str] = []
    real_create_task = asyncio.create_task

    def spy(coro, **kw):
        code = getattr(coro, "cr_code", None)
        names.append(getattr(code, "co_name", type(coro).__name__))
        return real_create_task(coro, **kw)

    monkeypatch.setattr(asyncio, "create_task", spy)
    with TestClient(svc.app):
        assert "_reap_loop" in names, (
            f"lifespan 起的后台任务里没有 _reap_loop（实际：{names}）——"
            f"空闲槽永远不会被回收"
        )


def test_reap_loop_reclaims_and_stops_daemon(svc, monkeypatch, tmp_path):
    """回收循环跑到到期槽时，必须走 _drop_slot（停 daemon）。"""
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)
    (tmp_path / "u1").mkdir()

    async def go():
        pool = svc._get_pool()
        pool.idle_ttl_s = 0
        await pool.acquire("u1")
        await pool.release("u1")
        task = asyncio.create_task(svc._reap_loop(pool, interval_s=0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return pool.size()

    assert asyncio.run(go()) == 0
    assert fake.destroyed == ["u1"]


def test_reap_loop_survives_exceptions(svc, monkeypatch, tmp_path):
    """回收循环不能因为一次异常就退出——那样槽再也不回收。"""
    fake = _FakeExecutor()
    monkeypatch.setattr(svc, "_get_executor", lambda: fake)

    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("模拟归档失败")

    async def go():
        pool = svc._get_pool()
        monkeypatch.setattr(pool, "reap_idle", boom)
        task = asyncio.create_task(svc._reap_loop(pool, interval_s=0.01))
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    assert calls["n"] >= 2, f"循环第一次出错后就退出了（只跑了 {calls['n']} 次）"

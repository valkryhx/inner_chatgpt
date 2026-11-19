"""池的测试（TDD RED）。

方案 §4.7 的核心断言都在这里。分成四组：

1. **槽的生命周期**：按需建、空闲回收、幂等
2. **纯净 fork**（§4.2 D）：快、且**不跨用户泄漏**
3. **OVERFLOW 三分支**（§4.7.3）：queue / evict / reject 行为必须真的不同
4. **上限**：`MAX_SLOTS` 是保护阀

⚠️ 第 2 组是本文件最重要的部分。方案 §4.3 实测过：
复用「执行过用户代码」的父进程会跨租户泄漏内存状态。
这条测试如果红，说明池实现退化成不安全的形态。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    from sandbox_service import workspace as ws_mod

    monkeypatch.setenv("HLZ_SANDBOX_WORKSPACES_DIR", str(tmp_path))
    monkeypatch.setattr(ws_mod, "DEFAULT_WORKSPACES_DIR", tmp_path)
    for uid in ("u1", "u2", "u3", "u4"):
        (tmp_path / uid).mkdir()
    return tmp_path


# ---------------------------------------------------------------- 槽生命周期


def test_no_slot_created_at_startup(ws_root):
    """启动时 0 个槽（方案 §4.7.6：不建议预建）。

    预建的空槽绑不上用户，等于白占内存。
    """
    from sandbox_service.pool import SlotPool

    pool = SlotPool(max_slots=3, workspace_resolver=lambda u: ws_root / u)
    assert pool.size() == 0


def test_slot_created_on_demand(ws_root):
    from sandbox_service.pool import SlotPool

    pool = SlotPool(max_slots=3, workspace_resolver=lambda u: ws_root / u)
    asyncio.run(pool.acquire("u1"))
    assert pool.size() == 1
    assert "u1" in pool.users()


def test_same_user_reuses_slot(ws_root):
    """同一用户第二次拿到的必须是同一个槽（证明复用生效）。"""
    from sandbox_service.pool import SlotPool

    pool = SlotPool(max_slots=3, workspace_resolver=lambda u: ws_root / u)

    async def go():
        a = await pool.acquire("u1")
        b = await pool.acquire("u1")
        return a, b

    a, b = asyncio.run(go())
    assert a is b
    assert pool.size() == 1


def test_slot_binds_its_own_workspace(ws_root):
    """槽绑定的 workspace 必须是该用户的（跨租户隔离的基础）。"""
    from sandbox_service.pool import SlotPool

    pool = SlotPool(max_slots=3, workspace_resolver=lambda u: ws_root / u)
    slot = asyncio.run(pool.acquire("u1"))
    assert slot.workspace == ws_root / "u1"


def test_destroy_is_idempotent(ws_root):
    from sandbox_service.pool import SlotPool

    pool = SlotPool(max_slots=3, workspace_resolver=lambda u: ws_root / u)
    asyncio.run(pool.acquire("u1"))
    asyncio.run(pool.destroy("u1"))
    asyncio.run(pool.destroy("u1"))  # 再来一次不报错
    assert pool.size() == 0


def test_reap_idle_removes_expired(ws_root):
    """空闲超时的槽被回收（方案 §4.7.4 的 TTL 分支）。"""
    from sandbox_service.pool import SlotPool

    pool = SlotPool(
        max_slots=3, workspace_resolver=lambda u: ws_root / u, idle_ttl_s=0.05
    )

    async def go():
        await pool.acquire("u1")
        await pool.release("u1")
        await asyncio.sleep(0.12)
        return await pool.reap_idle()

    reaped = asyncio.run(go())
    assert reaped == 1
    assert pool.size() == 0


def test_reap_does_not_remove_busy_slot(ws_root):
    """正在用的槽不许被回收——否则会掐断正在跑的代码。"""
    from sandbox_service.pool import SlotPool

    pool = SlotPool(
        max_slots=3, workspace_resolver=lambda u: ws_root / u, idle_ttl_s=0.05
    )

    async def go():
        await pool.acquire("u1")   # 不 release = 忙碌
        await asyncio.sleep(0.12)
        return await pool.reap_idle()

    reaped = asyncio.run(go())
    assert reaped == 0
    assert pool.size() == 1


# ---------------------------------------------------------------- MAX_SLOTS + OVERFLOW


def test_queue_evicts_idle_slot_when_owner_released(ws_root):
    """queue 在「有**空闲**槽」时必须能通过淘汰它来推进。

    ## 这条测试的期望值是**改过两次**的，两次都说明原因

    **第一次**写「queue 模式下 u1 的槽应该还在」（字面语义是"等"不"淘汰"）。
    实测发现那样会**永远等不到**：槽是专属的，u1 的槽仍然占名额，
    u2 等多久都不会有空位。

    **第二次**写成 u1 **acquire 之后不 release**，期望 u2 能拿到槽。
    这次失败得对——`u1` 此时是**忙碌**的，而「忙碌槽永不淘汰」是硬规则。
    所以这个期望本身违反方案 §4.7.4 的硬约束。

    正确形态是：u1 用完并**释放**后，u2 进来 → 淘汰 u1 的空闲槽 → u2 拿到。
    """
    from sandbox_service.pool import SlotPool

    pool = SlotPool(
        max_slots=1, workspace_resolver=lambda u: ws_root / u,
        overflow="queue", queue_timeout_s=2.0,
    )

    async def go():
        await pool.acquire("u1")
        await pool.release("u1")          # ★ 关键：释放后才是「空闲槽」
        s2 = await pool.acquire("u2")
        return s2

    s2 = asyncio.run(go())
    assert s2.user_id == "u2"
    assert pool.size() == 1
    assert "u1" not in pool.users(), "空闲槽应被淘汰以腾出名额"


def test_queue_waits_when_no_idle_slot(ws_root):
    """所有槽都忙时，queue 真的会等（而不是立刻失败）。"""
    from sandbox_service.pool import SlotPool, SlotBusy

    pool = SlotPool(
        max_slots=1, workspace_resolver=lambda u: ws_root / u,
        overflow="queue", queue_timeout_s=0.2,
    )

    async def go():
        await pool.acquire("u1")          # 忙碌，不 release
        t0 = asyncio.get_event_loop().time()
        with pytest.raises(SlotBusy) as ei:
            await pool.acquire("u2")
        elapsed = asyncio.get_event_loop().time() - t0
        return elapsed, ei.value

    elapsed, exc = asyncio.run(go())
    # 必须是「等够了才超时」，不是立刻失败
    assert elapsed >= 0.15, f"没等就失败了（{elapsed:.3f}s）"
    assert exc.queue_wait_ms >= 150, f"queue_wait_ms={exc.queue_wait_ms}"


def test_queue_times_out(ws_root):
    """queue 超时要报错，不能无限等。"""
    from sandbox_service.pool import SlotPool, SlotBusy

    pool = SlotPool(
        max_slots=1, workspace_resolver=lambda u: ws_root / u,
        overflow="queue", queue_timeout_s=0.1,
    )

    async def go():
        await pool.acquire("u1")
        with pytest.raises(SlotBusy):
            await pool.acquire("u2")

    asyncio.run(go())


def test_evict_removes_lru_idle_slot(ws_root):
    """evict：淘汰最久未用的**空闲**槽。"""
    from sandbox_service.pool import SlotPool

    pool = SlotPool(
        max_slots=2, workspace_resolver=lambda u: ws_root / u, overflow="evict"
    )

    async def go():
        await pool.acquire("u1"); await pool.release("u1")
        await asyncio.sleep(0.01)
        await pool.acquire("u2"); await pool.release("u2")
        await asyncio.sleep(0.01)
        # u1 是更久未用的 → 应被淘汰
        await pool.acquire("u3")
        return pool.users()

    users = asyncio.run(go())
    assert "u1" not in users
    assert "u3" in users
    assert pool.size() == 2


def test_evict_never_removes_busy_slot(ws_root):
    """★ evict 也**不许**淘汰忙碌槽——这是硬规则，不是可配置项。"""
    from sandbox_service.pool import SlotPool

    pool = SlotPool(
        max_slots=1, workspace_resolver=lambda u: ws_root / u,
        overflow="evict", queue_timeout_s=0.1,
    )

    async def go():
        await pool.acquire("u1")               # 忙碌（不 release）
        with pytest.raises(Exception):          # 全忙 → 退化成 queue → 超时
            await pool.acquire("u2")
        assert "u1" in pool.users(), "忙碌槽被淘汰了！"

    asyncio.run(go())


def test_evict_falls_back_to_queue_when_all_busy(ws_root):
    """evict 且全忙 → 退化成 queue（方案 §4.7.4 第 3 条）。"""
    from sandbox_service.pool import SlotPool, SlotBusy

    pool = SlotPool(
        max_slots=1, workspace_resolver=lambda u: ws_root / u,
        overflow="evict", queue_timeout_s=0.15,
    )

    async def go():
        await pool.acquire("u1")
        t0 = asyncio.get_event_loop().time()
        with pytest.raises(SlotBusy):
            await pool.acquire("u2")
        waited = asyncio.get_event_loop().time() - t0
        # 必须是「等了一会儿才失败」，不是立刻 reject
        assert waited >= 0.1, f"没等就失败了（{waited:.3f}s）说明退化成 reject 了"

    asyncio.run(go())


def test_reject_fails_immediately(ws_root):
    """★ reject：立刻失败，**不排队**。"""
    from sandbox_service.pool import SlotPool, SlotBusy

    pool = SlotPool(
        max_slots=1, workspace_resolver=lambda u: ws_root / u,
        overflow="reject", queue_timeout_s=5.0,
    )

    async def go():
        await pool.acquire("u1")
        t0 = asyncio.get_event_loop().time()
        with pytest.raises(SlotBusy):
            await pool.acquire("u2")
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.1, f"reject 应该立刻失败，实际等了 {elapsed:.3f}s"

    asyncio.run(go())


def test_unknown_overflow_falls_back_to_queue(ws_root):
    """非法 overflow 退回 queue（比退回 reject 安全：请求最终能成功）。"""
    from sandbox_service.pool import SlotPool

    pool = SlotPool(
        max_slots=1, workspace_resolver=lambda u: ws_root / u, overflow="nonsense"
    )
    assert pool.overflow == "queue"


# ---------------------------------------------------------------- 纯净 fork


def test_slot_parent_never_executes_user_code(ws_root):
    """★ 核心安全断言：父进程不能执行用户代码。

    方案 §4.3 实测：fork 是写时复制，子进程**能读到**父进程的全部内存。
    如果父进程执行过用户 A 的代码，之后 fork 给 B 的子进程就读得到 A 的残留。
    """
    from sandbox_service.pool import SlotPool

    pool = SlotPool(max_slots=2, workspace_resolver=lambda u: ws_root / u)

    async def go():
        slot = await pool.acquire("u1")
        # 槽的父进程只应该做「启动 + 预热」，不该有在跑的用户代码
        assert not slot.has_user_code_loaded, (
            "槽的父进程加载了用户代码——这会跨用户泄漏内存状态"
        )

    asyncio.run(go())


def test_slot_child_uses_exit_not_atexit(ws_root):
    """子进程必须 os._exit()，不跑父进程的 atexit。

    这条用静态检查实现：读**执行器**的源码，要求出现 os._exit。

    ⚠️ 检查的是 `executor/bwrap.py`，不是 `pool.py`：
    池负责调度（谁用哪个槽），执行器负责 fork 出子进程跑代码。
    写在池里说明职责搞混了——本测试原本指错了文件（已修）。
    """
    src = (BACKEND / "sandbox_service" / "executor" / "bwrap.py").read_text(
        encoding="utf-8"
    )
    assert "os._exit" in src, (
        "子进程必须 os._exit()——正常 return 会跑父进程的 atexit/lockfile 释放逻辑"
    )


def test_preload_list_is_fixed_not_per_request(ws_root):
    """预导入清单必须来自配置，不能按请求动态 import。

    按请求 import 会把用户数据带进父进程（等于退化成不安全的形态）。
    """
    from sandbox_service.pool import SlotPool

    pool = SlotPool(max_slots=2, workspace_resolver=lambda u: ws_root / u)
    # 源码层面：apply 里不应有基于 code 内容的 import 逻辑
    src = (BACKEND / "sandbox_service" / "pool.py").read_text(encoding="utf-8")
    assert "_preload" in src
    assert "exec(" not in src.split("def exec")[0], "池层不该执行用户代码"
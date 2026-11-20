r"""跑测试不许在 Docker 里留容器（TDD RED）。

## 为什么有这条测试

2026-11-20 真机发现本机积了 **59 个 `hlz_sbx_*` 容器**（57 running），
其中 **58 个挂载的是 pytest 的 tmp_path**：

    C:\Users\drago\AppData\Local\Temp\pytest-of-drago\pytest-1711\...\u1:/ws

即每一轮 pytest 都在 Docker 里留一个 `sleep infinity` 的 1GB 容器。
泄漏到 1711 轮才被发现，是因为它在内存上几乎看不出来
（真实写入 4.1kB，`docker ps --size` 的 562MB 是共享镜像层的 virtual size）。

## 三重失效（都在测试路径上，所以能潜伏）

1. `pool.reap_loop` 只在 `manager.start()` 里 `create_task`，唯一调用点是
   `app/main.py:122` —— **测试从不经过 main.py**，所以回收协程从没跑过；
2. `conftest._reset_app_sandbox_singletons._clear()` 只把 `_manager` 置 None，
   **不调 `shutdown()`**。而 `ContainerPool` 是**跨测试存活的模块级单例**
   （`pool._pool`），`_entries` 里登记的容器 ID 随 `_manager` 一起失联；
3. `probe.cleanup_orphans` 写得很完整（含多实例 `live_owners` 保护），
   但只在 `start()` 里被调，且 `backend != "inproc"` 时直接 return ——
   测试恰好走 inproc，永远不触发。

## 判据设计（判据必须能区分两种状态）

**必须看「池里登记过什么 + 有没有被销毁」，不能看 Docker 实时状态。**

- 看 Docker：slow，且依赖本机装了 Docker SDK / 跑得动容器 —— CI 上会 skip，
  于是「修没修」读数相同 ⇒ 不是判据。
- 查全局 `pool._pool` 与 `runtime.destroy` 调用记录：任何环境都能跑，
  且「有销毁记录」与「没有销毁记录」读数不同 ⇒ 是真判据。

用假 runtime 注入（`destroy` 只记录不真删），避免测试自己造容器 ——
否则「测试泄漏」这条测试会自己泄漏。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _RecordingRuntime:
    """只记录 destroy 调用，不碰 Docker。"""

    def __init__(self) -> None:
        self.destroyed: list[str] = []
        self.created = 0

    def is_alive(self, cid: str) -> bool:
        return True

    def create(self, user_id: str, host_workspace: str) -> str:
        self.created += 1
        return f"cid_{user_id}_{self.created}"

    def destroy(self, cid: str) -> None:
        self.destroyed.append(cid)


def _pool_with(*cids: str):
    """造一个「里面已经登记了容器」的池，runtime 换成记录器。"""
    from app.sandbox import pool as pool_mod

    rt = _RecordingRuntime()
    p = pool_mod.ContainerPool(runtime=rt)
    for i, cid in enumerate(cids):
        p._entries[f"u{i}"] = pool_mod.ContainerEntry(
            user_id=f"u{i}", container_id=cid, created_at=0.0,
            last_used=0.0, in_flight=0)
    return p, rt

# ---------------------------------------------------------------- 核心判据

def test_pool_shutdown_destroys_registered_containers():
    """★ 锁住 `shutdown()` 的语义：清空注册表 + 逐个真销毁。

    这是 conftest teardown 要调的那个入口。
    """
    p, rt = _pool_with("cid_a", "cid_b")
    assert p.size() == 2
    n = asyncio.run(p.shutdown())
    assert n == 2, f"shutdown 应销毁 2 个，实际 {n}"
    assert sorted(rt.destroyed) == ["cid_a", "cid_b"], (
        f"runtime.destroy 没被逐个调用：{rt.destroyed}")
    assert p.size() == 0, "注册表没清空"


def test_pool_destroy_user_destroys_that_container():
    """按用户销毁也要真的 destroy（`/stop` 走这条）。"""
    p, rt = _pool_with("cid_x", "cid_y")
    ok = asyncio.run(p.destroy_user("u0"))
    assert ok is True
    assert rt.destroyed == ["cid_x"], f"实际 {rt.destroyed}"
    assert p.size() == 1, "只该少一条"


# ---------------------------------------------------------------- conftest 守卫

def test_conftest_teardown_calls_pool_shutdown():
    """★ RED：`conftest` 的沙箱单例重置必须在 teardown 调 `pool.shutdown()`。

    ## 判据为什么必须是 AST 而不是文本

    本仓库踩过：`assert "shutdown" in src` 在「只写在注释里」时也绿。
    所以用 AST 找出 `_reset_app_sandbox_singletons` 里的**调用节点**，
    要求存在 `shutdown` 这个 `Attribute` 调用。
    """
    import ast

    src = Path("backend/tests/conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = None
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_reset_app_sandbox_singletons"):
            fn = node
            break
    assert fn is not None, "conftest 里找不到 _reset_app_sandbox_singletons"

    calls: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            nm = getattr(f, "attr", "") or getattr(f, "id", "")
            if nm:
                calls.append(nm)

    assert ("shutdown" in calls) or ("release_pool_sync" in calls), (
        "conftest 的沙箱单例重置没有销毁池里的容器 —— "
        "它们会随 _manager 一起失联，Docker 里留下 sleep infinity。\n"
        f"当前调用：{sorted(set(calls))}")

def test_conftest_teardown_actually_destroys():
    """行为测试：模拟 conftest 的 teardown 跑一遍，容器必须被销毁。

    比 AST 更进一步 —— AST 只能证明「调了 shutdown 这个名字」，
    不能证明「调的是池的那个 shutdown、且真的销毁了」。
    """
    from app.sandbox import pool as pool_mod

    rt = _RecordingRuntime()
    p = pool_mod.ContainerPool(runtime=rt)
    p._entries["u1"] = pool_mod.ContainerEntry(
        user_id="u1", container_id="cid_leak", created_at=0.0, last_used=0.0)
    pool_mod._pool = p          # 注入全局单例，复刻 conftest 能拿到的那个

    asyncio.run(pool_mod.get_pool().shutdown())

    assert rt.destroyed == ["cid_leak"], (
        f"teardown 后容器没被销毁：{rt.destroyed}；"
        "这就是 58 个 hlz_sbx_* 的来源")
    pool_mod._pool = None       # 清理，别污染别的测试


# ---------------------------------------------------------------- 判据自检

def test_judge_can_distinguish_two_states():
    """判据自检：不销毁 vs 销毁，读数必须不同。

    否则本文件所有断言都是假判据（本仓库踩过一次：判据自己重新推导
    了产品的错误假设，于是永远绿）。
    """
    from app.sandbox import pool as pool_mod

    # 状态 A：只丢弃引用，不 shutdown —— 正是现在的 bug
    rt_a = _RecordingRuntime()
    p_a = pool_mod.ContainerPool(runtime=rt_a)
    p_a._entries["u1"] = pool_mod.ContainerEntry(
        user_id="u1", container_id="cid_a", created_at=0.0, last_used=0.0)
    p_a = None                       # 丢弃引用（复刻 _manager = None）
    assert rt_a.destroyed == [], "状态 A 不该有销毁记录"

    # 状态 B：调 shutdown —— 修复后的行为
    rt_b = _RecordingRuntime()
    p_b = pool_mod.ContainerPool(runtime=rt_b)
    p_b._entries["u1"] = pool_mod.ContainerEntry(
        user_id="u1", container_id="cid_b", created_at=0.0, last_used=0.0)
    asyncio.run(p_b.shutdown())
    assert rt_b.destroyed == ["cid_b"], "状态 B 必须有销毁记录"

    assert rt_a.destroyed != rt_b.destroyed, "两种状态读数相同 ⇒ 判据无效"


# ---------------------------------------------------------------- 挂死守卫

def test_teardown_shutdown_does_not_hang_on_stuck_lock():
    """★ RED：池锁被「死掉的协程」持有时，teardown 不许永久挂起。

    ## 这是什么风险（实测出来的，不是设想）

    `ContainerPool.shutdown()` 里是 `async with self._lock`。如果用裸
    `asyncio.run(pool.shutdown())`，而锁在上一个测试的事件循环里被某个
    中途退出的协程持有（`await lock.acquire()` 之后没 release），
    那么新循环里的 `acquire()` **会永久等待**。

    实测（`work/_probe_lock4.py`，有界 3s 复现）：

        locked=True → wait_for(3s) → TIMEOUT

    裸 await 的后果就是整个 pytest 会话卡死 —— 比泄漏容器严重得多。
    修法是 `wait_for` 加超时：拿不到锁就放弃销毁，至少不挂死。

    ## 判据

    造一个锁被持有的池，带超时调 teardown 用的那个入口；
    必须**在有限时间内返回**，哪怕它什么都没销毁。
    """
    from app.sandbox import pool as pool_mod

    rt = _RecordingRuntime()
    p = pool_mod.ContainerPool(runtime=rt)
    p._entries["u1"] = pool_mod.ContainerEntry(
        user_id="u1", container_id="cid_stuck", created_at=0.0, last_used=0.0)

    # 在别的循环里把锁拿走不放（模拟中途死掉的协程）
    asyncio.run(p._lock.acquire())

    from app.sandbox import leak_guard

    # 必须在超时内返回，不许挂死
    n = asyncio.run(leak_guard.release_pool(p, timeout_s=2.0))
    assert n == 0, (
        f"锁被占时应当放弃并在超时后返回 0，实际 {n}")
    # 而且真的返回了（能执行到这一行本身就是判据）

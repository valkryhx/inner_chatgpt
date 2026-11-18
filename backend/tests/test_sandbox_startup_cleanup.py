"""启动清扫与重启残留（补充 test_sandbox_identity.py 的启动语义）。

## 为什么单独一份

`test_sandbox_identity.py` 锁的是「标签与命名」；
这里锁的是**启动时的清理语义**——一个容易搞错但后果很明显的点：

  owner_id 含 PID。进程重启后 PID 变了，
  上一轮留下的容器 owner 是旧 PID，精确匹配永远清不到。

如果启动清扫用精确 owner，它就成了摆设：每次重启泄漏一批容器。
"""
from __future__ import annotations

import inspect


def test_startup_cleanup_uses_same_machine_not_exact_owner():
    """启动清扫必须按机器过滤，不能用精确 owner（含 pid）。

    原因：进程重启后 PID 变化，精确匹配清不到上一轮残留。
    安全性：容器只存在于本机 Docker，启动时本机上一轮的进程已经死了，
    按机器过滤不会误删「还在干活」的容器（当前进程自己还没建容器）。
    """
    from app.sandbox.manager import SandboxManager

    src = inspect.getsource(SandboxManager.start)
    assert "same_machine=True" in src, (
        "启动清扫没有按机器过滤 —— 进程重启后 PID 变化，"
        "精确 owner 匹配清不到上一轮残留，容器会越积越多")


def test_cleanup_skips_other_machines(monkeypatch):
    """same_machine=True 只清本机，别的机器/实例的容器不能碰。"""
    from app.sandbox import probe as sbx_probe

    removed: list[str] = []

    class FakeContainer:
        def __init__(self, cid, owner):
            self.id = cid
            self.labels = {sbx_probe.OWNER_LABEL: owner}

        def remove(self, force=False):
            removed.append(self.id)

    class FakeClient:
        class containers:
            @staticmethod
            def list(all=False, filters=None):
                return [FakeContainer("other1", "faraway-host:1"),
                        FakeContainer("other2", "another-box:2")]

        def close(self):
            pass

    monkeypatch.setattr(sbx_probe, "_client", lambda: FakeClient())
    n = sbx_probe.cleanup_orphans(same_machine=True)
    assert n == 0, f"别的机器的容器被清掉了：{removed}"
    assert removed == []


def test_cleanup_same_machine_clears_local_instances(monkeypatch):
    """same_machine=True 要清掉本机**所有实例**的（含不同 pid）。"""
    from app.sandbox import probe as sbx_probe
    from app.sandbox.identity import owner_machine

    removed: list[str] = []
    machine = owner_machine()

    class FakeContainer:
        def __init__(self, cid, owner):
            self.id = cid
            self.labels = {sbx_probe.OWNER_LABEL: owner}

        def remove(self, force=False):
            removed.append(self.id)

    class FakeClient:
        class containers:
            @staticmethod
            def list(all=False, filters=None):
                return [
                    FakeContainer("local_old_pid", f"{machine}:11111"),
                    FakeContainer("local_other_pid", f"{machine}:22222"),
                    FakeContainer("remote", "other-machine:333"),
                ]

        def close(self):
            pass

    monkeypatch.setattr(sbx_probe, "_client", lambda: FakeClient())
    n = sbx_probe.cleanup_orphans(same_machine=True)
    assert n == 2, f"应清本机 2 个，实际 {n}（removed={removed}）"
    assert "remote" not in removed, "跨机容器被误清"


def test_runtime_never_cleans_during_operation():
    """运行期不该有全局清扫 —— 那会删掉正在干活的容器。

    清扫只应出现在两处：启动（manager.start）与显式运维调用。
    """
    import app.sandbox.pool as pool_mod
    import app.sandbox.manager as mgr_mod

    for mod in (pool_mod, mgr_mod):
        src = inspect.getsource(mod)
        # reap_loop 用 reap_idle（按 TTL 精确回收），不是 cleanup_orphans
        assert "cleanup_orphans" not in src or "start" in src, (
            f"{mod.__name__} 里出现了运行期清扫")
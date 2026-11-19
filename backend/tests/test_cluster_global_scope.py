"""跨机存活集合 & 日志文件名唯一性。

## 事故 1：每台机器用各自 hostname 做 Redis key（真跑 compose 复现）

```
app-1 (hostname d1ae4fbe797e) -> zadd hlz:instances:d1ae4fbe797e
app-2 (hostname 4c7af2741a10) -> zadd hlz:instances:4c7af2741a10
```

两个实例永远在**各自的键**里，`live_owners()` 只返回自己那一个：

- `/api/health` 报 `live_instances=1`（实际 2 个）
- `is_generation_dead_elsewhere` 看不到远程 owner -> 把别人正在跑的
  generation 判死 -> **数据分叉**（这正是多实例要修的 bug 之一）
- `recover_stale_runtime` 会把另一个实例正在跑的任务收敛掉

根因是一个函数承担了两种作用域：

| 调用方 | 需要的作用域 |
| --- | --- |
| `probe.cleanup_orphans`（清残留容器） | **本机**（容器只在本机 Docker 里） |
| `is_generation_dead_elsewhere`（判活） | **全集群** |
| `recover_stale_runtime`（收敛） | **全集群** |

修法：改成**单一全局键** + 按 owner 前缀过滤。

## 事故 2：两个容器的 app.log 撞名

日志文件名用 `app-<tag>.log`，而 tag 在没注入 `HLZ_INSTANCE_ID` 时是 PID。
两个容器的 python PID 可能相同（实测都拿到 7），于是共享卷上写同一个文件。

修法：文件名用**完整 owner**（含 hostname），不只是 tag。

## 反向确认

- `live_owners` 恢复成「按本机键读」 -> `test_live_owners_sees_other_hosts` 变红
- 日志文件名退回只用 tag -> `test_app_log_name_includes_host` 变红
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

REDIS_URL = os.environ.get("HLZ_TEST_REDIS_URL", "")
needs_redis = pytest.mark.skipif(
    not REDIS_URL, reason="未设置 HLZ_TEST_REDIS_URL，跳过 Redis 集成测试")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def with_redis(monkeypatch):
    from app import cluster
    from app.cluster import instances

    monkeypatch.setenv("HLZ_REDIS_URL", REDIS_URL)
    monkeypatch.setenv("HLZ_REDIS_PREFIX", f"hlzms{uuid.uuid4().hex[:8]}")
    cluster.reset_for_tests()
    instances.reset_for_tests()
    yield
    cluster.reset_for_tests()
    instances.reset_for_tests()


# ---------------------------------------------------------------- 跨机可见

@needs_redis
def test_live_owners_sees_other_hosts(with_redis, monkeypatch):
    """**核心**：不同 hostname 的实例必须互相看得见。

    这就是 compose `--scale` 的真实拓扑 —— 每个副本 hostname 不同。
    看不见对方 -> 跨实例判活整个失效 -> 数据分叉。
    """
    from app.cluster import instances

    async def go():
        redis = await instances.get_redis()
        assert redis is not None
        key = instances._key()
        now = int(time.time() * 1000)
        await redis.zadd(key, {"hostA:1": now, "hostB:2": now, "hostC:3": now})
        await redis.pexpire(key, 60_000)
        return await instances.live_owners()

    got = run(go())
    assert got == {"hostA:1", "hostB:2", "hostC:3"}, f"看不到其它机器的实例：{got}"


@needs_redis
def test_live_owners_filters_by_machine(with_redis):
    """按机器过滤仍要能工作（容器清扫只关心本机）。"""
    from app.cluster import instances

    async def go():
        redis = await instances.get_redis()
        key = instances._key()
        now = int(time.time() * 1000)
        await redis.zadd(key, {"hostA:1": now, "hostB:2": now})
        await redis.pexpire(key, 60_000)
        return await instances.live_owners("hostA")

    assert run(go()) == {"hostA:1"}


@needs_redis
def test_register_from_two_hosts_both_visible(with_redis, monkeypatch):
    """两个「不同机器」的实例各自登记后，任一方都能看到 2 个。"""
    from app.sandbox import identity
    from app.cluster import instances

    async def go():
        real_host = identity._hostname()
        monkeypatch.setattr(identity, "_hostname", lambda: "podA")
        await instances.register_self()
        monkeypatch.setattr(identity, "_hostname", lambda: "podB")
        monkeypatch.setattr(instances, "_TOKEN", "other-proc")
        await instances.register_self()
        monkeypatch.setattr(identity, "_hostname", real_host)
        return await instances.live_owners()

    got = run(go())
    assert len(got) == 2, f"两个不同主机的实例没都登记上：{got}"
    assert any(o.startswith("podA:") for o in got)
    assert any(o.startswith("podB:") for o in got)


@needs_redis
def test_claim_key_is_global(with_redis, monkeypatch):
    """认领键不能带机器前缀 —— 否则同 owner 跨机也发现不了冲突。

    断言的是**行为**：换一个「机器」后 key 必须不变。
    只查字符串里有没有 hostname 不够 —— 实现可能用别的机器名写法。
    """
    from app.cluster import instances
    from app.sandbox import identity

    real = identity._hostname()
    monkeypatch.setattr(identity, "_hostname", lambda: "hostA")
    k1 = instances._claim_key("hostA:1")
    monkeypatch.setattr(identity, "_hostname", lambda: "hostB")
    k2 = instances._claim_key("hostA:1")
    monkeypatch.setattr(identity, "_hostname", real)
    assert k1 == k2, f"认领键随机器名变了：{k1} != {k2}"
    assert k1 == instances._claim_key("hostA:1")


@needs_redis
def test_unregister_only_removes_self(with_redis, monkeypatch):
    """注销只能删自己那一行，不能连带删别人的。"""
    from app.cluster import instances
    from app.sandbox import identity

    async def go():
        redis = await instances.get_redis()
        key = instances._key()
        now = int(time.time() * 1000)
        await redis.zadd(key, {"otherhost:9": now})
        monkeypatch.setattr(identity, "_hostname", lambda: "me")
        await instances.unregister_self()
        return await instances.live_owners()

    assert run(go()) == {"otherhost:9"}


# ---------------------------------------------------------------- 日志文件名

def test_app_log_name_includes_host(monkeypatch, tmp_path):
    """**核心**：日志文件名必须含 hostname。

    只含 tag 时，两个容器的 PID 相同（实测都拿到 7）就会写同一个文件；
    共享 `appdata` 卷上两个进程交错写同一份日志，且无法区分。
    """
    import logging
    from app import logging_setup

    root = logging.getLogger()
    team = logging.getLogger(logging_setup.TEAM_LOGGER_NAME)
    prelude = logging.getLogger(logging_setup.PRELUDE_LOGGER_NAME)
    saved_root = list(root.handlers)
    saved_level = root.level
    saved_team = list(team.handlers)
    saved_team_prop = team.propagate
    saved_prelude = list(prelude.handlers)
    saved_prelude_prop = prelude.propagate
    logging_setup._configured = False
    monkeypatch.setattr(logging_setup, "_logs_dir", lambda: tmp_path)
    try:
        monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
        monkeypatch.delenv("HLZ_APP_LOG", raising=False)
        monkeypatch.delenv("HLZ_INSTANCE_ID", raising=False)
        monkeypatch.setattr("app.sandbox.identity._hostname", lambda: "hostX")
        cfg = logging_setup.setup_logging()
        name = Path(cfg["app_log"]).name
        assert "hostX" in name, f"日志文件名没有 hostname：{name}（多容器会撞名）"
    finally:
        root.handlers[:] = saved_root
        root.setLevel(saved_level)
        team.handlers[:] = saved_team
        team.propagate = saved_team_prop
        prelude.handlers[:] = saved_prelude
        prelude.propagate = saved_prelude_prop
        logging_setup._configured = False


def test_app_log_names_differ_for_same_pid_different_host(monkeypatch, tmp_path):
    """同一 PID、不同 hostname -> 必须是两个文件（真跑 compose 时的实际情形）。"""
    import logging
    from app import logging_setup
    from app.sandbox import identity

    root = logging.getLogger()
    team = logging.getLogger(logging_setup.TEAM_LOGGER_NAME)
    prelude = logging.getLogger(logging_setup.PRELUDE_LOGGER_NAME)
    saved_root = list(root.handlers)
    saved_level = root.level
    saved_team = list(team.handlers)
    saved_team_prop = team.propagate
    saved_prelude = list(prelude.handlers)
    saved_prelude_prop = prelude.propagate

    monkeypatch.setattr(logging_setup, "_logs_dir", lambda: tmp_path)
    monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
    monkeypatch.delenv("HLZ_INSTANCE_ID", raising=False)
    monkeypatch.setattr(os, "getpid", lambda: 7)

    def _reset():
        # 必须把**所有**通道的 handler 都清掉：team / prelude 是独立 logger，
        # 只清 root 会让它们累积多份 handler —— 后续测试的文件里会多出
        # 上一次的残留行，表现为「单跑绿、组合跑红」。
        logging_setup._configured = False
        root.handlers[:] = saved_root
        root.setLevel(saved_level)
        team.handlers[:] = saved_team
        team.propagate = saved_team_prop
        prelude.handlers[:] = saved_prelude
        prelude.propagate = saved_prelude_prop

    try:
        monkeypatch.setattr(identity, "_hostname", lambda: "aaa")
        a = Path(logging_setup.setup_logging()["app_log"]).name
        _reset()
        monkeypatch.setattr(identity, "_hostname", lambda: "bbb")
        b = Path(logging_setup.setup_logging()["app_log"]).name
        assert a != b, f"两个容器算出同一个日志文件名：{a}"
    finally:
        _reset()
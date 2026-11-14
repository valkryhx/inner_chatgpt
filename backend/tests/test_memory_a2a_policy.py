"""M5：记忆在 A2A 三档下的边界（L1 单测）。

## 为什么这组测试必须存在

`tool_policy` 的保证是「运行期靠 registry 里没有该工具兜底」。但记忆有**三条**
通道，只有第一条被 registry 覆盖：

| 通道 | 受 registry 管辖？ |
|---|---|
| ① 模型主动调 `memory_write` / `memory_list` | ✅ 有 |
| ② 读侧注入（`MemoryInjector` / 选择器预取） | ❌ 无 —— 不走 registry |
| ③ 写侧后台提取（`on_turn_end` 钩子） | ❌ 无 —— 不走 registry |

②③ 都不经过工具注册表。所以「`chat_only` 拿不到任何工具」不等于
「`chat_only` 不会读写记忆」。这组测试把三条通道逐档钉住。

## 为什么 ② 在 read_only/chat_only 下应该**保留**

读侧注入不是「调用者的能力」，而是「这个用户自己的背景事实」。A2A 是一个
受信代理在替该用户说话，它知道用户的偏好是对用户有益的，且不会外泄到别的
用户（注入器闭包捕获 token 绑定的 `user_id`）。把它掐掉会让 read_only
档变成一个「不记得用户是谁」的残缺代理，而没换来任何安全收益——
记忆的读边界是 user_id，token 已经绑死了。

对照 CC：CC 的 MCP 记忆库在只读场景下同样**可读不可写**。

## 为什么 ③ 在 read_only/chat_only 下必须**关闭**

后台提取会**写**记忆。只读 token 触发写入，等于它拿到了一个绕过 registry 的
写入通道 —— 而且写入的内容会在**下一个会话**里被当成用户事实注入，
是一次持久的、用户看不见的副作用。这正是「只读」二字要禁止的事。
"""

from __future__ import annotations

import asyncio

import pytest

from app.tool_policy import POLICY_CHAT_ONLY, POLICY_FULL, POLICY_READ_ONLY, allows


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 通道 ①：registry 层（已有行为，钉住防回归）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("policy,read_ok,write_ok", [
    (POLICY_FULL, True, True),
    (POLICY_READ_ONLY, True, False),
    (POLICY_CHAT_ONLY, False, False),
])
def test_registry_is_the_tool_boundary(policy, read_ok, write_ok):
    assert allows("memory_list", policy) is read_ok
    assert allows("memory_read", policy) is read_ok
    assert allows("memory_write", policy) is write_ok


# --------------------------------------------------------------------------
# 通道 ②：读侧注入 —— full / read_only 保留，chat_only 掐掉
# --------------------------------------------------------------------------

def test_read_injection_injector_itself_is_policy_agnostic(tmp_path, monkeypatch):
    """注入器类本身不判档位 —— 判据在**安装点**（`allows_memory_read`）。

    为什么把闸放在安装点而不是注入器里：`MemoryInjector` 是纯读组件，
    它只该关心「有没有记忆可注入」。把它做成档位敏感的，会让
    「直接构造注入器」这类正当用法（team 路径、测试、将来的其它入口）
    都要自己再传一次 policy，闸就会散成好几处、迟早漏一处。
    """
    from app import memory_store as ms
    from app.memory_inject import MemoryInjector

    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "mem")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("HLZ_MEMORY_ENABLED", "1")
    monkeypatch.setenv("HLZ_MEMORY_SELECTOR", "0")
    run(ms.init_tables())
    run(ms.write_memory("u1", slug="user-name", title="称呼",
                        description="用户希望被称呼为老徐", body="# 老徐\n",
                        kind="user"))

    got = run(MemoryInjector("u1")())
    assert got and "老徐" in got[0]["content"]


@pytest.mark.parametrize("policy,read_ok", [
    (POLICY_FULL, True),
    (POLICY_READ_ONLY, True),
    (POLICY_CHAT_ONLY, False),
])
def test_memory_read_gate_matrix(policy, read_ok):
    """三档阶梯必须是严格包含的：chat_only 连记忆都不该看到。"""
    from app.tool_policy import allows_memory_read

    assert allows_memory_read(policy) is read_ok


@pytest.mark.parametrize("policy,write_ok", [
    (POLICY_FULL, True),
    (POLICY_READ_ONLY, False),
    (POLICY_CHAT_ONLY, False),
])
def test_memory_write_gate_matrix(policy, write_ok):
    from app.tool_policy import allows_memory_write

    assert allows_memory_write(policy) is write_ok


# --------------------------------------------------------------------------
# 通道 ③：写侧后台提取 —— 只有 full 可以
# --------------------------------------------------------------------------

@pytest.fixture()
def turn_runtime_factory(tmp_path, monkeypatch):
    """造一个能跑 build_turn_runtime 的隔离环境，返回 (build, base)。"""
    from app import workspace
    from app.subagent import store as sub_store
    from app.subagent.session import build_turn_runtime

    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "s.db")
    run(sub_store.init_tables())

    from app.subagent.context import AgentContext
    from app.tools import Tool, ToolRegistry

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
        registry = ToolRegistry([mk("spawn_agent")])

    ctx = AgentContext(user_id="u1", session_id="s1", message_id="m1",
                       generation_id="g1")
    return build_turn_runtime, FakeBase(), ctx


@pytest.mark.parametrize("policy,expect_hook", [
    (POLICY_FULL, True),
    (POLICY_READ_ONLY, False),
    (POLICY_CHAT_ONLY, False),
])
def test_extraction_hook_respects_tool_policy(turn_runtime_factory, policy, expect_hook):
    """后台提取会写记忆 —— 只读/纯对话档不许挂这个钩子。

    它是唯一的「不经过 registry 的写入通道」：不挂钩子，就没有任何东西
    能在本轮结束后往记忆库里写东西。
    """
    build, base, ctx = turn_runtime_factory
    turn = run(build(base, ctx, tool_policy=policy))
    assert (turn.loop.on_turn_end is not None) is expect_hook, (
        f"{policy} 档的提取钩子挂载情况不对（期望 {expect_hook}）")


def test_read_only_turn_still_gets_memory_injector(turn_runtime_factory):
    """反过来也要钉住：只读档不能因为裁剪而把读侧注入一起丢掉。"""
    build, base, ctx = turn_runtime_factory
    turn = run(build(base, ctx, tool_policy=POLICY_READ_ONLY))
    assert getattr(turn.loop, "_memory_injector", None) is not None
    # 共享去重集合仍要挂上（M3.5 的预取靠它避免重复注入）
    assert getattr(turn.loop, "_memory_injected_slugs", None) is not None


def test_chat_only_turn_has_no_memory_injector(turn_runtime_factory):
    """chat_only 连记忆都不注入 —— 它的契约是「什么都不碰」。

    三档必须是严格包含的阶梯。若最严的一档反而是唯一能看到用户私人
    记忆的档，这个阶梯就断了。
    """
    build, base, ctx = turn_runtime_factory
    turn = run(build(base, ctx, tool_policy=POLICY_CHAT_ONLY))
    assert getattr(turn.loop, "_memory_injector", None) is None


def test_sse_turn_end_installer_respects_policy():
    """SSE 层那一侧的同名闸（非 team 路径 / 防御性）。

    turn 路径上 ``loop._memory_aware=True`` 已经让 SSE 让位，所以这道闸
    平时走不到。但它必须正确：一旦哪天有人给非 team 路径也传 tool_policy，
    没有这道闸就又是一条无 registry 覆盖的写通道。
    """
    from app.sse import _install_memory_turn_end

    class FakeLoop:
        message_injector = None
        on_turn_end = None

    class FakeUR:
        def __init__(self, policy):
            self.loop = FakeLoop()
            self.user_id = "u1"
            self.model = None
            self.tool_policy = policy

    for policy, expect in ((POLICY_FULL, True), (POLICY_READ_ONLY, False),
                           (POLICY_CHAT_ONLY, False)):
        ur = FakeUR(policy)
        _install_memory_turn_end(ur, "u1")
        assert (ur.loop.on_turn_end is not None) is expect, policy


def test_sse_injector_installer_respects_chat_only():
    """SSE 侧的读闸：chat_only 不装记忆注入器，read_only / full 要装。"""
    from app.sse import _install_memory_injector

    class FakeLoop:
        message_injector = None

    class FakeUR:
        def __init__(self, policy):
            self.loop = FakeLoop()
            self.user_id = "u1"
            self.generation_id = "g1"
            self.tool_policy = policy

    for policy, expect in ((POLICY_FULL, True), (POLICY_READ_ONLY, True),
                           (POLICY_CHAT_ONLY, False)):
        ur = FakeUR(policy)
        _install_memory_injector(ur, "u1", "s1", "hi")
        assert (getattr(ur.loop, "_memory_injector", None) is not None) is expect, policy

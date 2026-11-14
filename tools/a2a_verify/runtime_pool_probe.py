"""核实 RuntimeHub 到底是「池子」还是「无上限缓存」（只读，不改源码）。

问题来源：用户问「agent runtime 是否已经做成了一个池子，是不是有自动扩容」。

要回答三件事，必须分别测：
  1. 有没有**复用**（同一 (user, model) 是否拿回同一个对象）
  2. 有没有**上限/淘汰**（用户数增长时字典会不会无限长）
  3. 所谓「自动扩容」实际是什么机制（按需惰性创建 vs 预分配/弹性伸缩）

做法：直接调 RuntimeHub，**但把真贵的 build 换成一个只记账的假实现** ——
真实 build 会连 MCP、建 httpx client，几十个用户跑起来太慢，而这些与本问题无关
（本问题问的是「缓存结构」，不是「build 有多贵」）。

跑法：cd backend && python ../tools/a2a_verify/runtime_pool_probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
os.environ.setdefault("HLZ_MEMORY_ENABLED", "0")

from app import runtime as rt  # noqa: E402


class FakeUR:
    """只记账，不真的建 client / 连 MCP。"""
    built = 0

    def __init__(self) -> None:
        self.closed = False

    async def build(self, user_id, llm_config_id=None):
        FakeUR.built += 1
        self.user_id = user_id
        return self

    async def close(self):
        self.closed = True


async def main() -> int:
    rt.UserRuntime = FakeUR          # 只在本进程替换，不改源码
    hub = rt.RuntimeHub()
    hub.store = object()

    print("=" * 62)
    print("1) 复用：同一 (user, model) 是否返回同一个对象")
    print("=" * 62)
    a1 = await hub.get_user_runtime("u1", "")
    a2 = await hub.get_user_runtime("u1", "")
    print(f"   同一 key 两次: same object = {a1 is a2}")
    print(f"   FakeUR.build 调用次数 = {FakeUR.built}  （期望 1）")
    b1 = await hub.get_user_runtime("u1", "llm_other")
    print(f"   换 model: 新对象 = {b1 is not a1}  （期望 True，key 含 model）")

    print()
    print("=" * 62)
    print("2) 上限：用户数增长时缓存会不会无限长")
    print("=" * 62)
    for i in range(200):
        await hub.get_user_runtime(f"bulk_{i}", "")
    n = len(hub._runtimes)
    print(f"   200 个新用户后 _runtimes 条数 = {n}")
    print(f"   build 总次数 = {FakeUR.built}")
    print(f"   → 无上限/无淘汰的证据: {n >= 200}")

    print()
    print("=" * 62)
    print("3) 并发：同一 key 同时来 5 个请求会不会重复 build（_locks 的作用）")
    print("=" * 62)
    before = FakeUR.built
    res = await asyncio.gather(*[hub.get_user_runtime("race", "") for _ in range(5)])
    print(f"   build 增量 = {FakeUR.built - before}  （期望 1）")
    print(f"   5 个结果同一对象 = {len({id(r) for r in res}) == 1}")

    print()
    print("=" * 62)
    print("4) 回收：只有显式 invalidate 才会 drop")
    print("=" * 62)
    n0 = len(hub._runtimes)
    await hub.invalidate("u1")
    n1 = len(hub._runtimes)
    print(f"   invalidate('u1') 前 {n0} → 后 {n1}  （该用户的全部 model 都被 drop）")
    print(f"   a1.closed = {a1.closed}")
    await hub.invalidate_all()
    print(f"   invalidate_all 后 = {len(hub._runtimes)}")

    print()
    print("=" * 62)
    print("结论")
    print("=" * 62)
    print("   - 复用：有（按 (user_id, model_id) 缓存，并发用 per-key asyncio.Lock）")
    print("   - 上限/淘汰/LRU：**没有**。_runtimes 随 (用户×模型) 组合无限增长")
    print("   - 所谓「自动扩容」= 首次访问时惰性 build，不是预分配也不是弹性伸缩")
    print("   - 回收只来自显式 invalidate / invalidate_all（配置变更时触发）")
    print("   - 每个存活条目持有：httpx client + MCP 连接 + registry + AgentLoop")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

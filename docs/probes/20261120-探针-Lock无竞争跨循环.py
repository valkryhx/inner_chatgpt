"""探针：pool.shutdown() 能不能在「另一个 asyncio.run」里跑。

这决定了修法：teardown 里能不能直接 asyncio.run(pool.shutdown())。
不实测就写进 docstring 是猜。
"""
import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

class FakeRuntime:
    def __init__(self): self.destroyed = []
    def destroy(self, cid): self.destroyed.append(cid)

class Pool:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.entries = {"u1": "cid_aaa", "u2": "cid_bbb"}
        self.runtime = FakeRuntime()
    async def release_style(self):          # 模拟测试里对 lock 的第一次 await
        async with self.lock:
            return len(self.entries)
    async def shutdown(self):
        async with self.lock:
            es = list(self.entries.values())
            self.entries.clear()
        for e in es:
            await asyncio.to_thread(self.runtime.destroy, e)
        return len(es)

p = Pool()
print("1) 第一个循环里 await 一次 lock：", asyncio.run(p.release_style()))
print("2) 循环已关闭，现在用新循环调 shutdown()")
try:
    n = asyncio.run(p.shutdown())
    print(f"   ✓ 成功，销毁 {n} 个；runtime 记录 {p.runtime.destroyed}")
except RuntimeError as e:
    print(f"   ✗ RuntimeError: {e}")
    print("   ⇒ teardown 不能直接 asyncio.run(pool.shutdown())")
except Exception as e:
    print(f"   ✗ {type(e).__name__}: {e}")

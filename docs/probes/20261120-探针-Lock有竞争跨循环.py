"""探针 2：Lock 在**有竞争**时是否绑定 loop；绑定后再跨循环用会怎样。

我在 conftest 的 docstring 里写了「无竞争走快速路径不调 _get_loop」。
这个说法必须实测确认，否则 docstring 是猜的。
"""
import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

async def contended_use(lock):
    """制造竞争：两个协程同时抢锁。"""
    async def worker():
        async with lock:
            await asyncio.sleep(0)
    await asyncio.gather(worker(), worker())

lock = asyncio.Lock()
print("1) 第一个循环里制造竞争")
asyncio.run(contended_use(lock))
print(f"   跑完后 lock._loop = {getattr(lock, '_loop', '<无此属性>')}")

print("2) 换一个新循环用同一个 lock")
async def use_again():
    async with lock:
        return "acquired"
try:
    print("   ", asyncio.run(use_again()))
except RuntimeError as e:
    print(f"   ✗ RuntimeError: {e}")

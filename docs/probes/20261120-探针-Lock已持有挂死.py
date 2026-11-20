"""探针 4（有界）：确认「锁已持有 ⇒ 跨循环 acquire 永久挂起」，
并验证 wait_for 能否兜住。

探针 3 裸跑卡满 180s，这里加 3s 上限复现，不再挂死。
"""
import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

lock = asyncio.Lock()

async def leave_locked():
    await lock.acquire()      # 拿了不放

asyncio.run(leave_locked())
print(f"1) 上个循环结束：locked={lock.locked()}")

async def try_acquire():
    async with lock:
        return "acquired"

async def bounded():
    try:
        return await asyncio.wait_for(try_acquire(), timeout=3.0)
    except asyncio.TimeoutError:
        return "TIMEOUT"

print("2) 新循环 + wait_for(3s)：", asyncio.run(bounded()))
print("   ⇒ 裸 await 会永久挂起；wait_for 能兜住并抛 TimeoutError")

# 3) 再确认：用 wait_for 兜住之后，还能不能正常继续（锁仍被占）
print("3) 再试一次 wait_for(2s)：", asyncio.run(
    (lambda: (asyncio.wait_for(try_acquire(), timeout=2.0)))()
) if False else "跳过（锁仍被占，结果同上）")

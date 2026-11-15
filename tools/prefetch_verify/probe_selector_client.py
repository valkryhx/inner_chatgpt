import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# tools/prefetch_verify/ 下：parents[2] 是仓库根
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

async def main():
    from app import memory_prefetch as mp, db
    print("enabled:", mp.prefetch_enabled())
    print("selector_model:", repr(mp.selector_model()))
    uid = "u_d2c15b9cfd07"
    c = await mp.selector_client_for(uid)
    print("client:", type(c).__name__ if c else None)
    if c:
        await c.aclose()

asyncio.run(main())

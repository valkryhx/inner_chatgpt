import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# tools/prefetch_verify/ 下：parents[2] 是仓库根
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["HLZ_MEMORY_SELECTOR"] = "1"
os.environ["HLZ_MEMORY_SELECTOR_MODEL"] = "moma/deepseek-latest-flash"

async def main():
    from app.memory_inject import install_memory_injector
    from app import memory_prefetch as mp
    class FakeLoop:
        message_injector=None
        user_id="u_test"
    loop=FakeLoop()
    inj = install_memory_injector(loop,"u_test")
    print("shared set exists:", hasattr(loop,"_memory_injected_slugs"))
    print("shared id:", id(loop._memory_injected_slugs) if hasattr(loop,"_memory_injected_slugs") else None)
    f = mp._make_prefetch_factory(None, "u_test")
    print("factory built")
    h = f(loop, [{"role":"user","content":"问题一句"}])
    print("handle:", type(h).__name__ if h else None)
    st = loop._memory_prefetch_state
    print("prefetch injected id:", id(st.injected))
    print("same object:", st.injected is loop._memory_injected_slugs)

asyncio.run(main())

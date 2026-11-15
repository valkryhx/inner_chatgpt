import asyncio, os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
os.environ["HLZ_MEMORY_SELECTOR"]="0"

async def main():
    from app.agent_loop import AgentLoop
    from app.tools import ToolRegistry
    from app.llm_client import ChatResult

    class M:
        async def run(self, *a, **kw): return ChatResult(content="")

    reg = ToolRegistry([])
    N = 1000
    t0 = time.monotonic()
    for i in range(N):
        loop = AgentLoop(M(), reg, "sys", max_turns=300, user_id="u")
    dt = (time.monotonic()-t0)/N*1000
    print(f"AgentLoop 构造：{dt*1000:.1f} µs/次（{dt:.3f} ms）")

asyncio.run(main())

"""在真实进程环境里检查：prefetch_enabled 与 selector_client_for 的真实结果。"""
import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
os.environ["HLZ_MEMORY_SELECTOR"] = "1"
os.environ["HLZ_MEMORY_SELECTOR_MODEL"] = "moma/deepseek-latest-flash"
sys.path.insert(0, ".")

async def main():
    from app import memory_prefetch as mp
    print("prefetch_enabled:", mp.prefetch_enabled())
    uid = "u_5c2b468482e2"
    c = await mp.selector_client_for(uid)
    print("client:", type(c).__name__ if c else None)
    if c is None:
        # 查配置
        from app import db
        try:
            base = await db.get_default_llm_config(uid)
            print("default cfg:", {k: base.get(k) for k in ("id","model","base_url","api_key")} if base else None)
        except Exception as e:
            print("db err:", type(e).__name__, e)
    else:
        await c.aclose()

asyncio.run(main())

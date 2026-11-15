import asyncio, tempfile, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

async def main():
    tmp = Path(tempfile.mkdtemp())
    from app import memory_store as ms, db
    ms.MEMORIES_DIR = tmp / "memories"
    ms.DB_PATH = tmp / "app.db"
    db.DB_PATH = tmp / "app.db"
    await db.init_db()
    await ms.init_tables()
    await ms.write_memory("u", slug="a", title="a", description="d", body="# a\n\nx\n")
    page = await ms.list_memories("u", limit=10)
    for e in page.entries:
        print("list entry version:", repr(e.version))
    spec = await ms.get_memory("u", "a")
    print("get_memory version:", repr(spec.version))

asyncio.run(main())

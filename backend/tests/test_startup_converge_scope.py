"""启动收敛不得误伤**别的实例正在跑**的生成（2026-11-19 审计 §1）。

## 事故

`backend/app/db.py` 的 `init_db()` 末尾有一条**无条件**的启动自愈：

```python
UPDATE messages SET status='stopped'
WHERE role='assistant' AND status='streaming'
```

它的前提是「进程刚启动不可能有真实生成在跑」—— 这句只对**本进程**成立，
对整个集群不成立。而 `init_db()` 在**每个实例的 lifespan 里都执行**
（`main.py:48`），于是一次扩容就把现存实例正在跑的会话全部标成 stopped。

实测复现：`backend/tests/manual/probe_initdb_cross_instance_wipe.py`
（SQLite 与 PG 两种后端都 FAIL）。

## 这组测试钉什么

| 测试 | 钉什么 |
|---|---|
| `test_single_instance_still_converges` | 没配 Redis 时行为**完全不变**（单实例语义，这是迁移前行为，不能改） |
| `test_other_instance_generation_is_kept` | 配了 Redis 且 owner 还活着 -> **不许动** 那条 streaming |
| `test_dead_instance_generation_is_converged` | owner 已不在存活集合 -> 必须收敛（否则那条会话永远卡在 streaming） |
| `test_legacy_row_without_owner_is_converged` | owner 为空（升级前遗留）-> 收敛 |
| `test_local_generation_is_converged` | owner 是自己但本进程没有这个 job -> 收敛（上个进程崩溃遗留） |

## 为什么判据是「owner + 存活集合」而不是「job 在不在内存里」

`turn_job._JOBS` 只反映**本进程**。重启后必然为空，所以它区分不了
「上一进程崩溃遗留」和「别的实例正在跑」。只有落库的 owner 能区分。
这与 `team_store.recover_stale_runtime` 的判据完全一致 —— 那里已经做对了。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import db, sqlcompat  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """隔离库 + 一并改掉所有 import 了 DB_PATH 的模块（与 conftest 同口径）。"""
    path = tmp_path / "app.db"
    mods = [db]
    for name in ("team_store", "turn_marker"):
        try:
            mod = __import__(f"app.{name}", fromlist=["DB_PATH"])
            mods.append(mod)
        except Exception:  # pragma: no cover
            pass
    for m in mods:
        monkeypatch.setattr(m, "DB_PATH", path)
    run(db.init_db())
    yield path


async def _seed_streaming(path: Path, *, message_id: str, session_id: str = "s1",
                          user_id: str = "u1") -> None:
    """造一条 assistant streaming 消息（外加它依赖的 user / session）。"""
    async with sqlcompat.connect(path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users(id,email,name,username,password_hash,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, f"{user_id}@t.invalid", user_id, user_id, "x", db.now_ms()),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO sessions(id,user_id,title,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (session_id, user_id, "t", db.now_ms(), db.now_ms()),
        )
        await conn.execute(
            "INSERT INTO messages(id,session_id,role,content,created_at,status) "
            "VALUES(?,?,?,?,?,?)",
            (message_id, session_id, "assistant", "", db.now_ms(), "streaming"),
        )
        await conn.commit()


async def _status_of(path: Path, message_id: str) -> str:
    async with sqlcompat.connect(path) as conn:
        cur = await conn.execute("SELECT status FROM messages WHERE id=?", (message_id,))
        row = await cur.fetchone()
    return str(row[0]) if row else "<missing>"


async def _set_generation_owner(path: Path, *, user_id: str, session_id: str,
                               generation_id: str, owner: str) -> None:
    """把某个 generation 的 owner 写进 session_generations。"""
    from app import team_store
    await team_store.init_tables()
    async with sqlcompat.connect(path) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO session_generations"
            "(user_id,session_id,generation_id,status,started_at,owner) "
            "VALUES(?,?,?,'active',?,?)",
            (user_id, session_id, generation_id, db.now_ms(), owner),
        )
        await conn.commit()


# ------------------------------------------------------------------ 单实例不变

def test_single_instance_still_converges(iso):
    """没配 Redis（live_owners 为空）时，行为必须与迁移前完全一致：全清。

    这是回归保护：多实例的修正**不能改变**单机部署的行为。
    """
    run(_seed_streaming(iso, message_id="m_single"))

    async def go():
        # live_owners 为空集合 = 单实例语义
        await db.converge_stale_streaming(live_owners=set())
        return await _status_of(iso, "m_single")

    assert run(go()) == "stopped"


# ------------------------------------------------------------------ 活着的不动

def test_other_instance_generation_is_kept(iso):
    """owner 是别的实例且**还活着** -> 这条 streaming 不许动。

    这是本次审计的核心缺陷：原实现会把这条标成 stopped。
    """
    run(_seed_streaming(iso, message_id="m_alive", session_id="s_a", user_id="u_a"))

    async def go():
        await _set_generation_owner(iso, user_id="u_a", session_id="s_a",
                                    generation_id="g_a", owner="other-host:123")
        await db.converge_stale_streaming(live_owners={"other-host:123"})
        return await _status_of(iso, "m_alive")

    assert run(go()) == "streaming", \
        "别的实例正在跑的生成被误杀了 —— 这正是 §1 的 bug"


def test_local_generation_with_live_job_is_kept(iso):
    """owner 是我自己，且本进程确实有这个 job -> 不动。

    正常重启场景下这种情况不该出现（进程刚起没有 job），但它能锁住
    「判据必须看 owner + 存活集合，而不是无脑收敛」这个语义。
    """
    from app import turn_job

    run(_seed_streaming(iso, message_id="m_mine", session_id="s_m", user_id="u_m"))

    async def go():
        await _set_generation_owner(iso, user_id="u_m", session_id="s_m",
                                    generation_id="g_m", owner="me:1")
        job = turn_job.TurnJob("u_m", "s_m", "g_m")
        turn_job._JOBS[("u_m", "s_m", "g_m")] = job
        try:
            await db.converge_stale_streaming(live_owners={"me:1"},
                                              my_owner="me:1")
            return await _status_of(iso, "m_mine")
        finally:
            turn_job._JOBS.pop(("u_m", "s_m", "g_m"), None)

    assert run(go()) == "streaming"


# ------------------------------------------------------------------ 死了的要清

def test_dead_instance_generation_is_converged(iso):
    """owner 已不在存活集合 -> 必须收敛，否则那条会话永远卡在 streaming。"""
    run(_seed_streaming(iso, message_id="m_dead", session_id="s_d", user_id="u_d"))

    async def go():
        await _set_generation_owner(iso, user_id="u_d", session_id="s_d",
                                    generation_id="g_d", owner="gone-host:999")
        # 存活集合里没有 gone-host:999
        await db.converge_stale_streaming(live_owners={"alive-host:1"})
        return await _status_of(iso, "m_dead")

    assert run(go()) == "stopped"


def test_legacy_row_without_owner_is_converged(iso):
    """owner 为空（升级前遗留 / 没记 owner 的老数据）-> 收敛。

    这类行不可能有活实例在跑：升级前的 active 一定是残留。
    """
    run(_seed_streaming(iso, message_id="m_legacy", session_id="s_l", user_id="u_l"))

    async def go():
        # 不写 session_generations，或写了 owner=''
        await _set_generation_owner(iso, user_id="u_l", session_id="s_l",
                                    generation_id="g_l", owner="")
        await db.converge_stale_streaming(live_owners={"some-host:1"})
        return await _status_of(iso, "m_legacy")

    assert run(go()) == "stopped"


def test_no_active_generation_row_is_converged(iso):
    """有 streaming 消息但**没有任何 active generation 记录** -> 收敛。

    这是最常见的历史数据形态：老版本根本没写 session_generations。
    不收敛就会永远卡住。
    """
    run(_seed_streaming(iso, message_id="m_orphan", session_id="s_o", user_id="u_o"))

    async def go():
        await db.converge_stale_streaming(live_owners={"x:1"})
        return await _status_of(iso, "m_orphan")

    assert run(go()) == "stopped"


# ------------------------------------------------------------------ 不再误标记

def test_crash_marker_not_written_for_live_generation(iso):
    """回调「活着」的那条**不该**写崩溃标记。

    标记的语义是「上一进程没走完就死了」。别的实例正在跑不是崩溃，
    给它打标记会让下一轮的悬空 tool_call 被误判成「不能重放」。
    """
    from app import turn_marker

    run(_seed_streaming(iso, message_id="m_alive2", session_id="s_a2", user_id="u_a2"))
    run(turn_marker.init_tables())      # 表要先在，才能断言「里面没有这条」

    async def go():
        await _set_generation_owner(iso, user_id="u_a2", session_id="s_a2",
                                    generation_id="g_a2", owner="other:7")
        await db.converge_stale_streaming(live_owners={"other:7"})
        return await turn_marker.get_crash_marker("s_a2")

    assert run(go()) is None, "活着的生成被误标成崩溃了"
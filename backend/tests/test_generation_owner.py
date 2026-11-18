"""跨实例判活：**不能把别的实例正在跑的 generation 判死**。

## 事故场景（这是真 bug，不是理论）

```
实例 A 正在跑 g1（DB: active, owner=hostA:111）
用户在第二个标签页让请求落到实例 B
  B: acquire_generation 失败 -> active='g1'
     is_generation_live('g1') -> B 的内存里没有 -> False
     -> g1 判 stale + mark_stale_streaming_stopped
     -> A 还在写，B 已经把记录改了：数据分叉
```

修法：判 stale 前必须确认 **owner 已死**，不能只看本进程内存。

## 反向确认

把 `is_generation_dead_elsewhere` 里的存活集合检查去掉（改成恒 True），
`test_live_remote_owner_is_not_dead` 必须变红。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app import team_store
    monkeypatch.setattr(team_store, "DB_PATH", tmp_path / "app.db")
    run(team_store.init_tables())
    return team_store.DB_PATH


def _seed(db, gen: str, owner: str, *, status: str = "active"):
    from app import sqlcompat

    async def go():
        async with sqlcompat.connect(db) as conn:
            await conn.execute(
                "INSERT INTO session_generations"
                "(user_id,session_id,generation_id,status,started_at,owner) "
                "VALUES(?,?,?,?,?,?)",
                ("u1", "s1", gen, status, 1, owner))
            await conn.commit()

    run(go())


# ---------------------------------------------------------------- owner 判活

def test_live_remote_owner_is_not_dead(db, monkeypatch):
    """**核心**：owner 是别人、且它还活着 -> 绝不能判死。"""
    from app import generation_owner as go

    _seed(db, "g_live", "hostA:111")

    async def fake_live():
        return {"hostA:111"}

    monkeypatch.setattr(go, "live_owners", fake_live)
    assert run(go.is_generation_dead_elsewhere("u1", "s1", "g_live")) is False


def test_dead_remote_owner_is_dead(db, monkeypatch):
    """owner 是别人、但它已不在存活集合 -> 可以判死。"""
    from app import generation_owner as go

    _seed(db, "g_dead", "hostA:999")

    async def fake_live():
        return {"hostA:111"}

    monkeypatch.setattr(go, "live_owners", fake_live)
    assert run(go.is_generation_dead_elsewhere("u1", "s1", "g_dead")) is True


def test_empty_owner_is_dead(db, monkeypatch):
    """owner 空串（老库 / 升级前遗留）-> 判死。"""
    from app import generation_owner as go

    _seed(db, "g_old", "")

    async def fake_live():
        return {"hostA:111"}

    monkeypatch.setattr(go, "live_owners", fake_live)
    assert run(go.is_generation_dead_elsewhere("u1", "s1", "g_old")) is True


def test_own_owner_defers_to_caller(db, monkeypatch):
    """owner 是我自己 -> 这一步返回 False（自己的状态交给 is_generation_live）。"""
    from app import generation_owner as go
    from app.sandbox.identity import owner_id

    _seed(db, "g_mine", owner_id())

    async def fake_live():
        return {owner_id()}

    monkeypatch.setattr(go, "live_owners", fake_live)
    assert run(go.is_generation_dead_elsewhere("u1", "s1", "g_mine")) is False


def test_no_redis_degrades_to_single_instance(db, monkeypatch):
    """没配 Redis（存活集合为空）-> 除自己外都算死 = 单实例语义。"""
    from app import generation_owner as go

    _seed(db, "g_other", "somewhere:42")

    async def empty():
        return set()

    monkeypatch.setattr(go, "live_owners", empty)
    assert run(go.is_generation_dead_elsewhere("u1", "s1", "g_other")) is True


def test_missing_row_is_dead(db, monkeypatch):
    """查不到这一行 -> 判死（调用方可以安全接管）。"""
    from app import generation_owner as go

    async def fake_live():
        return {"hostA:111"}

    monkeypatch.setattr(go, "live_owners", fake_live)
    assert run(go.is_generation_dead_elsewhere("u1", "s1", "nope")) is True


# ---------------------------------------------------------------- 与 chat 集成

def test_chat_409_path_respects_remote_owner(db, monkeypatch):
    """chat.py 的 409 路径：别的实例在跑时不能判 stale，应该返回 409。"""
    import inspect
    from app.routers import chat

    src = inspect.getsource(chat)
    # 必须用 owner 判活，不能只凭 is_generation_live
    assert "is_generation_dead_elsewhere" in src or "owner" in src.lower(), \
        "chat.py 的 lease 抢占路径没有考虑跨实例 owner"


def test_is_generation_live_is_process_local():
    """钉住 `is_generation_live` 的语义：它是进程内的，不能当跨实例判据。

    这条不测行为，测**约束**：如果有人把它改成查 Redis，这个断言会红，
    提醒他 update 相关注释和 chat.py 的调用点（那里现在配合 owner 判断）。
    """
    import inspect
    from app import runtime

    src = inspect.getsource(runtime.is_generation_live)
    assert "_is_live_key" in src
    # 不能因为「顺手」就在里面加 Redis 查询——那会给每个 token 检查加往返
    assert "redis" not in src.lower()
    assert "await" not in src
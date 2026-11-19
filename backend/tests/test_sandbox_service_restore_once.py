"""归档恢复的**一次性**语义（TDD RED）。

## 这一个 bug 的实质

`_maybe_restore` 的 docstring 写得很清楚：

> 槽的生命周期内 workspace 是活的文件系统，反复解包会把用户的
> 新改动覆盖掉。所以只在**新建槽**（= 之前被回收/淘汰过）时恢复一次。

但 `exec_code` 里是无条件 `await _maybe_restore(slot)` —— **没有一次性守卫**。
所以它每执行一次代码就把归档解包一遍。

## 为什么平时看不出来

归档只在 `_evict` 时写，写的内容恰好等于当时的磁盘内容，
所以「解包回去」和「不动它」结果一样。这个 bug 被巧合掩盖了。

只要归档比磁盘旧，它就静默回滚用户数据。实测（容器内）：

    disk: CURRENT
    （放入旧归档）
    after 2nd exec: STALE     ← 用户写的 CURRENT 没了

归档比磁盘旧的现实场景：服务在写归档时崩溃（部分写入）、
将来接了对象存储从远端拉回旧版本、或者人工恢复了一份旧备份。

## 断言口径

不测「调用了几次 `_maybe_restore`」（那是实现细节），
而是测**用户数据有没有被覆盖** —— 这才是要保护的东西。
"""
from __future__ import annotations

import asyncio
import io
import sys
import tarfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def make_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    """干净的 main 模块：池 / 执行器 / 归档全部重置。

    ⚠️ 归档目录也必须关进 tmp_path。`VolBackend()` 的默认根是
    `/app/backend/data/sandbox-archives`（Windows 上是 `D:\\...`），
    不关就会让测例之间互相看见对方的归档 —— 本文件第一版就因此
    有一条用例假红（`test_no_archive_is_not_an_error` 看到了上一个
    测例写进去的 `d.txt`）。
    """
    from sandbox_service import main as svc_main

    monkeypatch.setenv("HLZ_SANDBOX_WORKSPACES_DIR", str(tmp_path))
    monkeypatch.setenv("HLZ_SANDBOX_ARCHIVE_DIR", str(tmp_path / "_archives"))
    from sandbox_service import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "DEFAULT_WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(svc_main, "_pool", None)
    monkeypatch.setattr(svc_main, "_executor", None)
    monkeypatch.setattr(svc_main, "_archive", None)
    return svc_main


class _EchoExecutor:
    """执行器替身：真的把 code 当 print 跑没必要，只记录调用。"""

    async def start(self, user_id):
        return True

    async def exec(self, user_id, code, *, code_type="python", timeout_s=120.0, cwd=""):
        from sandbox_service.executor.base import ExecResult

        return ExecResult(0, "", "", 1, False)

    async def destroy(self, user_id):
        pass

    async def health(self):
        return True, "ok"


def _put_archive(svc, user_id: str, files: dict[str, bytes]) -> None:
    from sandbox_service.archive import archive_key

    svc._get_archive().put(archive_key(user_id), make_tar(files))


def test_new_slot_restores_archive(svc, monkeypatch, tmp_path):
    """新建槽时**要**恢复归档（这是它存在的意义）。"""
    monkeypatch.setattr(svc, "_get_executor", lambda: _EchoExecutor())
    (tmp_path / "u1").mkdir()
    _put_archive(svc, "u1", {"old.txt": b"FROM_ARCHIVE"})

    async def go():
        pool = svc._get_pool()
        slot = await pool.acquire("u1")
        await svc._maybe_restore(slot)
        return slot

    slot = asyncio.run(go())
    assert (slot.workspace / "old.txt").read_bytes() == b"FROM_ARCHIVE"


def test_second_exec_does_not_restore_again(svc, monkeypatch, tmp_path):
    """★ 核心：同一个槽的**第二次** exec 不许再解包归档。

    这是被掩盖的那个 bug。断言用户新写的内容不能被归档覆盖。
    """
    monkeypatch.setattr(svc, "_get_executor", lambda: _EchoExecutor())
    (tmp_path / "u1").mkdir()
    _put_archive(svc, "u1", {"d.txt": b"STALE"})

    async def go():
        pool = svc._get_pool()
        slot = await pool.acquire("u1")
        await svc._maybe_restore(slot)              # 第一次：恢复 STALE
        (slot.workspace / "d.txt").write_bytes(b"CURRENT")   # 用户改写
        await svc._maybe_restore(slot)              # 第二次：不许再恢复
        await pool.release("u1")
        return slot

    slot = asyncio.run(go())
    assert (slot.workspace / "d.txt").read_bytes() == b"CURRENT", (
        "第二次 exec 又把归档解包了，用户写的 CURRENT 被旧归档 STALE 覆盖"
    )


def test_restore_flag_is_per_slot_not_per_user(svc, monkeypatch, tmp_path):
    """一次性标记要跟着**槽**走，不是跟着用户。

    槽被回收后再次 acquire 会新建一个槽对象 —— 那时**应该**恢复
    （因为磁盘目录可能在此期间被别的东西改过，恢复是它的职责）。
    如果标记挂在用户上，新槽就不会恢复了，同样错。
    """
    monkeypatch.setattr(svc, "_get_executor", lambda: _EchoExecutor())
    (tmp_path / "u1").mkdir()
    _put_archive(svc, "u1", {"d.txt": b"FROM_ARCHIVE"})

    async def go():
        pool = svc._get_pool()
        slot1 = await pool.acquire("u1")
        await svc._maybe_restore(slot1)
        await pool.release("u1")
        await pool.destroy("u1")                    # 槽没了
        slot2 = await pool.acquire("u1")            # 全新槽对象
        await svc._maybe_restore(slot2)
        await pool.release("u1")
        return slot1, slot2

    slot1, slot2 = asyncio.run(go())
    assert slot1 is not slot2
    # 新槽必须恢复过（旧内容回到磁盘）
    assert (slot2.workspace / "d.txt").read_bytes() == b"FROM_ARCHIVE"


def test_no_archive_is_not_an_error(svc, monkeypatch, tmp_path):
    """没有归档是正常状态（第一次用），不是错误。"""
    monkeypatch.setattr(svc, "_get_executor", lambda: _EchoExecutor())
    (tmp_path / "u1").mkdir()

    async def go():
        pool = svc._get_pool()
        slot = await pool.acquire("u1")
        await svc._maybe_restore(slot)
        await pool.release("u1")
        return slot

    slot = asyncio.run(go())
    assert list(slot.workspace.iterdir()) == []


def test_corrupt_archive_does_not_block_exec(svc, monkeypatch, tmp_path):
    """归档坏了不该让用户跑不了代码——记日志、当作没有归档。"""
    monkeypatch.setattr(svc, "_get_executor", lambda: _EchoExecutor())
    (tmp_path / "u1").mkdir()
    from sandbox_service.archive import archive_key

    svc._get_archive().put(archive_key("u1"), b"not a tar at all")

    async def go():
        pool = svc._get_pool()
        slot = await pool.acquire("u1")
        await svc._maybe_restore(slot)
        await pool.release("u1")
        return slot

    slot = asyncio.run(go())        # 不抛
    assert slot is not None


def test_archive_does_not_reset_restore_flag(svc, monkeypatch, tmp_path):
    """归档（evict 时）不许把「已恢复」标记清掉。

    ## 这条一开始写错了，记录一下

    第一版是这么测的：恢复 → 写 CURRENT → `_archive_slot` → 再恢复，
    断言文件仍是 CURRENT。它在**拆掉守卫的情况下照样绿** ——
    因为 `_archive_slot` 把 CURRENT 写进了归档，再解包回来当然还是
    CURRENT。断言和它声称要防的事情没有因果关系。

    改成直接盯字段：归档前后 `archived_restored` 必须一直是 True。
    这样「有人在归档回调里顺手重置标记」会立刻暴露。
    """
    monkeypatch.setattr(svc, "_get_executor", lambda: _EchoExecutor())
    (tmp_path / "u1").mkdir()
    _put_archive(svc, "u1", {"d.txt": b"STALE"})

    async def go():
        pool = svc._get_pool()
        slot = await pool.acquire("u1")
        await svc._maybe_restore(slot)
        assert slot.archived_restored is True, "恢复之后标记没置位"
        (slot.workspace / "d.txt").write_bytes(b"CURRENT")
        svc._archive_slot(slot)                       # 模拟 evict 前归档
        after = slot.archived_restored
        await pool.release("u1")
        return after

    assert asyncio.run(go()) is True, (
        "归档回调把「已恢复」标记清掉了——同一槽会重复解包"
    )
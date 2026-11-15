"""pytest 全局隔离：**任何测试都不许碰生产库**。

## 为什么需要这个文件（2026-11-15 实测事故）

项目里各模块都是 `from .config import DB_PATH`，**在 import 时就绑定了一份副本**：

    app/db.py            DB_PATH = <import 时的值>
    app/team_store.py    DB_PATH = <同一个对象的副本>
    app/subagent/store.py  ...

多数测试文件用 `isolated_db` fixture 逐个 `monkeypatch.setattr(module, "DB_PATH", tmp)`
把它们改掉。但 **6 个文件没做这件事**（`test_team_failover.py`、`test_team_hard_contract.py`、
`test_team_idempotency.py`、`test_team_persistent_worker.py`、`test_subagent_capabilities.py`、
`test_subagent_prompts.py`），其中 `test_team_persistent_worker` 会经
`SubagentRegistry.create_child` 真的写 `subagent_runs`。

后果：跑一次全量 `pytest tests`，生产库 `backend/data/app.db` 被写进 15 行测试垃圾
（10 条 `subagent_runs` + 2 条 `team_tasks` + 1 条 `team_events` + 2 条 `team_name_pins`，
`user_id` 全部指向不存在的用户）。这不是「可能存在」的风险，是 2026-11-15 真机发生的事实。

## 做法

一个 **session 级 autouse fixture**，在整个测试会话开始前把所有已知模块的
`DB_PATH` 指向同一个临时库、并建好表结构。逐用例的 `isolated_db` 仍然有效
（`monkeypatch` 在用例级覆盖，优先级更高，各自用各自的 tmp_path）。

## 为什么不用 monkeypatch

`monkeypatch` 是 function 作用域，会话级 fixture 里用它会在第一个用例结束时
就把补丁撤掉。所以这里手工保存原值、`yield` 之后还原。

## 为什么列模块名而不是遍历

遍历 `sys.modules` 找「有 DB_PATH 属性的模块」会把测试自己、第三方库也卷进来，
而且 `config.DB_PATH` 改了不会传导到已经 import 过它的模块（那是副本）。
显式列出**真的会写库**的模块，改错了能立刻看见。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 所有 import 了 `DB_PATH` 的运行时模块。**遗漏一个就是这个模块会写生产库。**
_DB_PATH_MODULES = (
    "app.db",
    "app.history_store",
    "app.memory_store",
    "app.team_store",
    "app.turn_marker",
    "app.a2a.store",
    "app.acp.store",
    "app.subagent.store",
    "app.config",
)


@pytest.fixture(scope="session", autouse=True)
def _isolate_production_db(tmp_path_factory):
    """把全进程的 DB_PATH 重定向到临时库，杜绝测试写生产库。"""
    import importlib

    tmp_db = tmp_path_factory.mktemp("global_db") / "app.db"
    saved: dict[str, object] = {}

    for name in _DB_PATH_MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 模块不存在/导入失败不影响隔离
            continue
        if hasattr(mod, "DB_PATH"):
            saved[name] = mod.DB_PATH
            mod.DB_PATH = tmp_db

    # 建表结构。**必须全部建**：没打 DB 补丁的那几个测试（它们以前直接用生产库
    # 已经建好的表）跑起来第一句就是 `no such table: subagent_runs` —— 这正说明
    # 它们以前一直在读写生产库。
    try:
        import asyncio

        from app import db as app_db
        from app import history_store as app_hs
        from app import memory_store as app_mem
        from app import team_store as app_team
        from app import turn_marker as app_tm
        from app.acp import store as app_acp_store
        from app.a2a import store as app_a2a_store
        from app.subagent import store as app_sub_store

        async def _init_all() -> None:
            await app_db.init_db()
            await app_team.init_tables()
            await app_sub_store.init_tables()
            await app_mem.init_tables()
            await app_tm.init_tables()
            await app_a2a_store.init_tables()
            await app_acp_store.init_tables()
            store = app_hs.HistoryStore()
            await store.init()
            app_hs.set_history_store(store)

        asyncio.run(_init_all())
    except Exception:  # noqa: BLE001 - 建表失败由具体用例暴露
        pass

    yield tmp_db

    for name, value in saved.items():
        try:
            mod = importlib.import_module(name)
            mod.DB_PATH = value
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="session", autouse=True)
def _no_real_network_for_llm():
    """兜底：禁止测试真的打外部 LLM（防止漏配 mock 时烧额度）。

    设成明显无效的地址，任何真调用都会立刻失败并暴露，而不是悄悄成功。
    """
    keys = ("AGENT_LLM_BASE_URL",)
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.setdefault(k, "http://127.0.0.1:9/__forbidden_in_tests__")
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

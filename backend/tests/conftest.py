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


# 记忆开关的**测试基线**。app/__init__.py 启动时会 load_dotenv(.env) 把开发者本地
# 配置灌进 os.environ —— 于是「默认值」类断言（如 test_status_endpoint 断言
# auto_write 默认 False）会随开发者本地 .env 而变化：2026-11-17 本地开发者为排障
# 把 HLZ_MEMORY_AUTO_WRITE 改成 1，全量测试就开始红，而代码没有任何问题。
# 这里在会话开始时删除所有 HLZ_MEMORY_* 变量，让「未设置」成为真正的默认状态；
# 用例仍可用 monkeypatch.setenv 逐个覆盖（那是 function 作用域，优先级更高）。
_MEMORY_ENV_PREFIX = "HLZ_MEMORY_"


@pytest.fixture(scope="session", autouse=True)
def _isolate_memory_env():
    """让记忆相关环境变量回到「未设置」，杜绝本地 .env 污染测试。"""
    saved = {k: v for k, v in os.environ.items() if k.startswith(_MEMORY_ENV_PREFIX)}
    for k in saved:
        os.environ.pop(k, None)
    yield
    # 还原：即使测试进程还要跑别的，也不该改掉调用方的环境
    for k in list(os.environ):
        if k.startswith(_MEMORY_ENV_PREFIX):
            os.environ.pop(k, None)
    os.environ.update(saved)

# ---------------------------------------------------------------- 沙箱服务单例隔离
#
# `sandbox_service.main` 有两个**进程内单例**：`_pool` 与 `_executor`。
# 它们是生产必需（daemon 就挂在执行器实例上，换实例 = 预热复不上），
# 但对测试是跨测例污染源：
#
#   测例 A `monkeypatch.setattr(svc_main, "_get_executor", lambda: fake_a)`
#   测例 B 没替换 → 直接拿到 fake_a（上一个测例的执行器）→ 断言全错
#
# 实测症状很误导：单跑 `test_sandbox_service_api.py` 全绿，混跑
# `-k sandbox` 时 3 个测例红（`IndexError: list index out of range` =
# fake_a.calls 是空的）。这与之前 `backends/base.py` 的模块级缓存
# 是同一类问题——本次直接按 function 作用域清，避免复发。
@pytest.fixture(autouse=True)
def _reset_sandbox_service_singletons():
    try:
        from sandbox_service import main as svc_main
    except Exception:  # noqa: BLE001 - 没装 sandbox_service 的测例不受影响
        yield
        return
    svc_main._pool = None
    svc_main._executor = None
    yield
    svc_main._pool = None
    svc_main._executor = None


# ---------------------------------------------------------------- 沙箱配置单例隔离
#
# `app.sandbox.config.get_config()` 与 `app.sandbox.manager.get_manager()`
# 都是进程内单例，且它们的值在**第一次调用时固化**：
#
#   get_config()  → load_config() 读环境变量
#   get_manager() → SandboxManager(cfg=get_config())
#
# 测例用 `monkeypatch.setenv("HLZ_SANDBOX_MODE", "strict")` + `get_config(reload=True)`
# 改掉它们之后，monkeypatch 在测例结束时**只回滚环境变量**，不会 reload 单例。
# 于是下一个测例拿到的是上一个测例留下的 strict manager。
#
# 实测症状：`test_skills_tools.py` 里 5 条 `code_run` 用例在混跑时红、
# 单跑时绿 —— 因为它们看到一个 mode=strict + 空白名单的沙箱配置。
#
# 这里按 function 作用域统一重置，与 `_reset_sandbox_service_singletons`
# （沙箱**服务**侧的单例）是同一类问题的两个地方。
@pytest.fixture(autouse=True)
def _reset_app_sandbox_singletons():
    try:
        from app.sandbox import config as sbx_config
        from app.sandbox import manager as sbx_manager
    except Exception:  # noqa: BLE001
        yield
        return

    def _clear(*, destroy: bool = True):
        """重置单例。**teardown 时必须先销毁池里的容器**。

        ## 为什么不能只 `_manager = None`（2026-11-20 实测事故）

        `ContainerPool` 是**模块级单例**（`pool._pool`），它比 `_manager`
        活得久。只把 `_manager` 置 None，池里 `_entries` 登记的容器 ID
        就再没人能拿到 —— 而 `pool.shutdown()` 是唯一会 destroy 它们的地方。
        于是每跑一轮走 Docker 路径的测试，Docker 里就多一个
        `sleep infinity` 容器。

        真机后果：本机积了 **59 个 `hlz_sbx_*`**（57 running），
        其中 58 个挂载的是 `pytest-of-.../pytest-NNNN/` 临时目录 ——
        每轮 pytest 一个。因为每容器真实写入只有 4.1kB，内存上看不出来，
        一直泄漏到 1711 轮才发现。

        ## 为什么 `setup` 也 destroy

        setup 时池里若还有容器，说明上一轮没清干净（比如上一个用例抛了
        SystemExit）。销毁它们比留着安全 —— 这些容器是测试专用的，
        宿主 workspace 指向 tmp_path，本来就不该活过一轮。

        ## 为什么经由 `leak_guard` 而不是直接 `asyncio.run(pool.shutdown())`

        **裸 shutdown 会挂死。** `shutdown()` 里是 `async with self._lock`；
        若锁在上一个事件循环里被某个中途退出的协程持有（`acquire()` 后
        没 `release`），新循环的 `acquire()` 会**永久等待**。实测
        （`work/_probe_lock4.py`）：`locked=True` 时 `wait_for(3s)` 直接
        TIMEOUT —— 裸 await 就是整个 pytest 会话卡死，比泄漏容器严重得多。

        `leak_guard.release_pool_sync` 带超时，拿不到锁就放弃销毁并打
        WARNING，保证 teardown 一定往下走。

        另外：跨事件循环本身是安全的 —— `asyncio.Lock` 在有竞争时才会
        绑定 loop，而即便绑定了旧 loop，新循环里 acquire 仍然能成功
        （实测 `work/_probe_lock2.py`）。真正的风险只有「锁已被持有」。
        """
        if destroy:
            try:
                from app.sandbox import leak_guard
                leak_guard.release_pool_sync(
                    sbx_manager.get_pool(), timeout_s=30.0)
            except Exception:  # noqa: BLE001 - 清理失败绝不能阻断 teardown
                pass
        sbx_config._cached = None
        sbx_manager._manager = None

    _clear()
    yield
    _clear()

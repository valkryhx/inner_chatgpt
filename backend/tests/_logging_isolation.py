"""日志配置的测试隔离：一处实现，所有用到 logging 的测试共用。

## 为什么要抽出来

`setup_logging()` 是**全局单例**（模块级 `_configured` + 三个 logger 的
handler 列表）。测试里如果只重置 root，不留神就会漏掉 team / prelude
这两个独立 logger，它们的 handler 会跨用例累积：

- 表现为「单跑绿、组合跑红」
- 更难查的是「文件里多几行上一次用例的残留」，断言 `rows[-1]` 时就错位

本仓库已踩过两次（`test_cluster_global_scope` 清得不够、
`test_multi_instance_observability` 的 fixture 同样漏了）。

## 用法

    def test_xxx(isolated_logging, tmp_path, monkeypatch):
        monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
        cfg = setup_logging()
        ...
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest


@contextmanager
def _restore_all():
    """保存并在退出时恢复所有 logging 全局状态。"""
    from app import logging_setup

    root = logging.getLogger()
    team = logging.getLogger(logging_setup.TEAM_LOGGER_NAME)
    prelude = logging.getLogger(logging_setup.PRELUDE_LOGGER_NAME)

    saved = {
        "root_handlers": list(root.handlers),
        "root_level": root.level,
        "root_filters": list(root.filters),
        "team_handlers": list(team.handlers),
        "team_propagate": team.propagate,
        "team_level": team.level,
        "prelude_handlers": list(prelude.handlers),
        "prelude_propagate": prelude.propagate,
        "prelude_level": prelude.level,
    }
    logging_setup._configured = False
    # **进入时也要清空**：前面的测试可能触发过 lifespan（TestClient 会），
    # 那已经把一批 handler 装到 root / team / prelude 上了。只把
    # `_configured` 置 False 不够 —— 再 setup 会**叠加**一批，
    # 旧 handler 的实例标识（可能是上个测试的 PID）会先输出，
    # 表现为「单跑绿、全量跑红」，且日志里出现别人的标识。
    root.handlers.clear()
    root.filters.clear()
    team.handlers.clear()
    prelude.handlers.clear()
    try:
        yield
    finally:
        root.handlers[:] = saved["root_handlers"]
        root.setLevel(saved["root_level"])
        root.filters[:] = saved["root_filters"]
        team.handlers[:] = saved["team_handlers"]
        team.propagate = saved["team_propagate"]
        team.setLevel(saved["team_level"])
        prelude.handlers[:] = saved["prelude_handlers"]
        prelude.propagate = saved["prelude_propagate"]
        prelude.setLevel(saved["prelude_level"])
        logging_setup._configured = False


@pytest.fixture()
def isolated_logging(monkeypatch, tmp_path):
    """把日志配置重定向到 tmp_path，退出时恢复全部全局状态。

    用法上等价于「干净进程里刚 import 完 logging_setup」。
    """
    from app import logging_setup

    with _restore_all():
        monkeypatch.setattr(logging_setup, "_logs_dir", lambda: tmp_path)
        yield tmp_path


@contextmanager
def fresh_logging_scope(tmp_path):
    """给非 pytest 场景（或需要多次 setup 的用例）用的上下文管理器。

    每次进出都完整重置，所以可以在同一个用例里 setup 两次
    （比如验证「两个不同实例标识写出两个不同文件」）。
    """
    from app import logging_setup

    with _restore_all():
        original = logging_setup._logs_dir
        logging_setup._logs_dir = lambda: tmp_path
        try:
            yield tmp_path
        finally:
            logging_setup._logs_dir = original
            # 上下文退出前把本次装的 handler 关掉，避免文件句柄泄漏
            # （Windows 上未关闭的句柄会让 tmp_path 清理失败）
            for lg in (logging.getLogger(),
                       logging.getLogger(logging_setup.TEAM_LOGGER_NAME),
                       logging.getLogger(logging_setup.PRELUDE_LOGGER_NAME)):
                for h in lg.handlers[:]:
                    if getattr(h, "_hlz_tmp", False):
                        try:
                            h.close()
                        except Exception:  # noqa: BLE001
                            pass
                        lg.removeHandler(h)
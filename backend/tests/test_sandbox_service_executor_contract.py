"""`Executor` 接口的契约测试（TDD RED）。

这一层先钉死**签名与语义**，不碰实现。理由是：阶段 4 要换成 gVisor 时，
只要这个接口没变，上层（HTTP / 池 / 归档）一行都不用改（方案 §8.2.3）。

所以本文件测的是「接口的形状」，不是「bwrap 能不能跑」——
后者在 `test_bwrap_executor.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_exec_result_is_a_named_tuple_with_required_fields():
    """ExecResult 必须是不可变的结构，字段齐全。

    为什么要求不可变：它要在 `asyncio.to_thread` 里返回、跨进程边界传，
    可变对象容易被下游就地修改（本仓库在别处踩过）。
    """
    from sandbox_service.executor.base import ExecResult

    r = ExecResult(
        exit_code=0, stdout="ok", stderr="", duration_ms=12, truncated=False
    )
    assert r.exit_code == 0
    assert r.stdout == "ok"
    assert r.stderr == ""
    assert r.duration_ms == 12
    assert r.truncated is False
    # 不可变
    with pytest.raises(Exception):
        r.exit_code = 1


def test_exec_result_fields_exactly_match_contract():
    """字段集合必须与方案 §8.2.1 完全一致，多一个少一个都不行。

    这条是防「实现时顺手加字段」——加字段会让 gVisor 实现漏填。
    """
    from sandbox_service.executor.base import ExecResult

    assert ExecResult._fields == (
        "exit_code",
        "stdout",
        "stderr",
        "duration_ms",
        "truncated",
    )


def test_protocol_declares_four_methods():
    """Protocol 必须有 start / exec / destroy / health 四个方法。"""
    from sandbox_service.executor.base import Executor

    for name in ("start", "exec", "destroy", "health"):
        assert hasattr(Executor, name), f"Executor 缺方法 {name}"


def test_bwrap_executor_satisfies_protocol():
    """BwrapExecutor 必须满足 Protocol（结构子类型）。

    `runtime_checkable` 才能 isinstance 检查；这条同时验证
    接口没有被写成「只有注释没有约束」。
    """
    from sandbox_service.executor.base import Executor
    from sandbox_service.executor.bwrap import BwrapExecutor

    assert issubclass(BwrapExecutor, Executor) or isinstance(
        BwrapExecutor(workspace_resolver=lambda uid: Path("/tmp") / uid),
        Executor,
    )


def test_inproc_executor_exists_and_satisfies_protocol():
    """`off` 模式要用的实现也必须存在（方案 §8.2.2）。"""
    from sandbox_service.executor.base import Executor
    from sandbox_service.executor.inproc import InprocExecutor

    ex = InprocExecutor()
    assert isinstance(ex, Executor)


def test_get_executor_rejects_unknown_backend():
    """未知 backend 必须报错，不能静默退回某个默认实现。

    静默退回会让「配错了但看起来在工作」——比直接报错危险得多。
    """
    from sandbox_service.executor import get_executor

    with pytest.raises(ValueError, match="unknown"):
        get_executor("nonsense")


def test_get_executor_defaults_to_bwrap(monkeypatch):
    """不指定时默认 bwrap（方案选 A）。"""
    from sandbox_service.executor import get_executor

    monkeypatch.delenv("HLZ_SANDBOX_EXECUTOR", raising=False)
    ex = get_executor()
    assert type(ex).__name__ == "BwrapExecutor"
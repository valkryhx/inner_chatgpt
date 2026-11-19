"""workspace 路径解析：单根 + 与 app 侧一致性。

## 这一组最重要的测试是最后一条

`test_matches_app_side_resolution` —— 用同一组输入分别跑 app 侧和
服务侧的解析函数，结果必须**完全相同**。

为什么这条最关键：两边各自独立实现（服务要能脱离 app 部署），
一旦规则漂移就会出现「app 读 A、沙箱写 B」的双视图错位（方案 §5）。

## 2026-11-19 收敛说明

本文件原来测「双根 + 白名单」：白名单用户落 `<sandbox 根>/<uid>`，
其余落 `<workspaces 根>/<uid>`。那套逻辑已整块删除，理由见
`sandbox_service/workspace.py` 的模块 docstring（双根是 Docker 方案的遗留，
bwrap 的 `--bind` 不需要它，而且它在生产里真的造成了路径错位）。

所以本文件现在测**单根**：任何用户都落同一个根，与任何沙箱开关无关。
双根遗留的断言全部删除，不再保留「白名单分支」的测试。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    """把 workspace 根指到 tmp（单根，没有第二个根可指了）。"""
    from sandbox_service import workspace as ws

    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv(ws.WORKSPACES_ENV, str(root))
    monkeypatch.setattr(ws, "DEFAULT_WORKSPACES_DIR", root)
    return root


# ---------------------------------------------------------------- 单根


def test_any_user_lands_in_the_single_root(dirs):
    from sandbox_service import workspace as ws

    for uid in ("u_ordinary", "u_vip", "u_c56b8337dd09"):
        assert ws.workspaces_root(uid) == dirs / uid


def test_whitelist_env_does_not_affect_path(dirs, monkeypatch):
    """★ 白名单**不能**影响路径。

    这是本次事故的直接回归：收敛前 `HLZ_SANDBOX_MVP_USERS` 在两个容器的
    compose 默认值不同，导致 app / sandbox 算出不同目录。
    现在路径与白名单完全解耦，设什么值都不该变。
    """
    from sandbox_service import workspace as ws

    uid = "u_c56b8337dd09"
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")
    before = ws.workspaces_root(uid)
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", uid)
    after = ws.workspaces_root(uid)
    assert before == after == dirs / uid


def test_dual_root_names_removed():
    """双根相关名字必须彻底不存在（防止有人又用回来）。"""
    from sandbox_service import workspace as ws

    for name in (
        "SANDBOX_WORKSPACES_ENV",
        "DEFAULT_SANDBOX_WORKSPACES_DIR",
        "sandbox_workspaces_dir",
        "mvp_users",
        "DEFAULT_MVP_USERS",
    ):
        assert not hasattr(ws, name), f"sandbox_service.workspace 里还有 {name}"


# ---------------------------------------------------------------- 路径安全


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b"])
def test_reject_unsafe_segment(bad):
    from sandbox_service.workspace import PathSecurityError, reject_unsafe_segment

    with pytest.raises(PathSecurityError):
        reject_unsafe_segment(bad)


@pytest.mark.parametrize("good", ["u_abc123", "u-c56b8337dd09"])
def test_accept_safe_segment(good):
    from sandbox_service.workspace import reject_unsafe_segment

    reject_unsafe_segment(good)


def test_workspaces_root_rejects_escape(dirs):
    from sandbox_service import workspace as ws

    with pytest.raises(ws.PathSecurityError):
        ws.workspaces_root("../escape")


# ---------------------------------------------------------------- 与 app 一致


def test_matches_app_side_resolution(dirs, monkeypatch):
    """★ 与 app 侧解析结果必须逐字相同。

    两边独立实现（服务要能脱离 app 部署），但结果必须一致，
    否则「两套视图」的问题会以另一种形式重现。

    做法：同一份环境 + 同一组 user_id，分别调两边的函数比对。
    """
    from app import workspace as app_ws

    monkeypatch.setattr(app_ws, "WORKSPACES_DIR", dirs)

    from sandbox_service import workspace as svc_ws

    for uid in ("u_vip", "u_ordinary", "u_c56b8337dd09"):
        a = app_ws.workspaces_root(uid)
        b = svc_ws.workspaces_root(uid)
        # 用 as_posix 比较：Windows 上 Path 渲染成反斜杠（假红）
        assert a.as_posix() == b.as_posix(), (
            f"user_id={uid} 两边解析不一致: app={a.as_posix()} svc={b.as_posix()}"
        )
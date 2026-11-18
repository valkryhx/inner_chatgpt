"""沙箱 config / policy / probe 的单测（设计文档 §8 第 1 步）。

这组测试锁的是**分流判据与三档语义**，不真起容器：
- `off` 时任何工具、任何用户都不走沙箱（零影响基线）；
- 白名单外用户即使 `strict` 也不走；
- 只读工具永远不走（读类留在进程内的产品决定）；
- 无 Docker 时 `probe` 返回原因而不是抛异常；
- 非法 mode 退回 `off`（安全默认）。
"""
from __future__ import annotations

import pytest

from app.sandbox import config as sbx_config
from app.sandbox import policy, probe


@pytest.fixture()
def fresh_cfg(monkeypatch):
    """每个用例重建配置缓存，避免单例串味。"""

    def _make(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        return sbx_config.get_config(reload=True)

    yield _make
    sbx_config.get_config(reload=True)


# ---------------- config 默认值 ----------------


def test_default_mode_is_off(monkeypatch):
    """铁律：默认关闭。任何人不配环境变量就必须是 off。"""
    monkeypatch.delenv("HLZ_SANDBOX_MODE", raising=False)
    cfg = sbx_config.get_config(reload=True)
    assert cfg.mode == "off"
    assert cfg.enabled is False


def test_invalid_mode_falls_back_to_off(fresh_cfg):
    cfg = fresh_cfg(HLZ_SANDBOX_MODE="yolo")
    assert cfg.mode == "off", "非法 mode 必须退回 off（安全默认）"


def test_default_image_and_users(monkeypatch):
    monkeypatch.delenv("HLZ_SANDBOX_IMAGE", raising=False)
    monkeypatch.delenv("HLZ_SANDBOX_MVP_USERS", raising=False)
    cfg = sbx_config.get_config(reload=True)
    # 断言等于常量而不是硬编码镜像名：换镜像（如换成本项目的 hlz-sandbox）
    # 不该让这条「未设置时取默认值」的测试变红。
    assert cfg.image == sbx_config.DEFAULT_IMAGE
    assert cfg.mvp_users == ("u_c56b8337dd09", "u_343a46896bff")


def test_users_parsed_and_trimmed(fresh_cfg):
    cfg = fresh_cfg(HLZ_SANDBOX_MVP_USERS=" a , b ,,c ")
    assert cfg.mvp_users == ("a", "b", "c")


def test_numeric_overrides_ignore_garbage(fresh_cfg):
    cfg = fresh_cfg(HLZ_SANDBOX_IDLE_TTL_S="not-a-number")
    assert cfg.idle_ttl_s == 900, "坏值必须退回默认，而不是 0 或负数"


# ---------------- policy 三档语义 ----------------


SANDBOXED = ["code_run", "file_write", "file_patch"]
NOT_SANDBOXED = [
    "file_read",
    "file_glob",
    "file_grep",
    "list_files",
    "load_skill",
    "memory_write",
    "some_mcp_tool",
]


def test_off_means_nothing_goes_to_sandbox(fresh_cfg):
    """零影响基线：off 时三个沙箱工具全部不走。"""
    cfg = fresh_cfg(HLZ_SANDBOX_MODE="off")
    for tool in SANDBOXED:
        assert policy.should_use_sandbox(tool, "u_c56b8337dd09", cfg) is False


def test_strict_sends_whitelisted_user(fresh_cfg):
    cfg = fresh_cfg(HLZ_SANDBOX_MODE="strict")
    for tool in SANDBOXED:
        assert policy.should_use_sandbox(tool, "u_c56b8337dd09", cfg) is True


def test_user_outside_whitelist_never_goes(fresh_cfg):
    """MVP 只影响 2 个人：其余 404 个用户行为必须零变化。"""
    cfg = fresh_cfg(HLZ_SANDBOX_MODE="strict")
    for tool in SANDBOXED:
        assert policy.should_use_sandbox(tool, "u_someone_else", cfg) is False


def test_read_only_tools_never_go(fresh_cfg):
    """读类留在进程内 —— 容器里没有 Skill 目录与仓库挂载点（设计文档 §5.1）。"""
    cfg = fresh_cfg(HLZ_SANDBOX_MODE="strict")
    for tool in NOT_SANDBOXED:
        assert policy.should_use_sandbox(tool, "u_c56b8337dd09", cfg) is False


def test_empty_whitelist_means_everyone(fresh_cfg):
    cfg = fresh_cfg(HLZ_SANDBOX_MODE="strict", HLZ_SANDBOX_MVP_USERS="")
    assert policy.should_use_sandbox("code_run", "u_anyone", cfg) is True


def test_sandboxed_tools_constant_is_exact():
    """钉住集合内容：改它等于改产品边界，必须有人显式看见。"""
    assert policy.SANDBOXED_TOOLS == {"code_run", "file_write", "file_patch"}


# ---------------- probe 不抛异常 ----------------


def test_probe_check_off_does_not_touch_docker(fresh_cfg):
    cfg = fresh_cfg(HLZ_SANDBOX_MODE="off")
    ok, reason = probe.check(cfg)
    assert ok is True and reason == ""


def test_docker_ready_returns_reason_not_raises(monkeypatch):
    """注入一个必然失败的 client 构造，验证返回 (False, reason) 而非抛异常。"""

    def _boom():
        from app.sandbox.errors import SandboxUnavailable

        raise SandboxUnavailable("模拟：无法连接 Docker")

    monkeypatch.setattr(probe, "_client", _boom)
    ok, reason = probe.docker_ready()
    assert ok is False
    assert "无法连接 Docker" in reason
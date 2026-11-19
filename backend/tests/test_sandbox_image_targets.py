"""沙箱镜像的「测试 target」：真 daemon 测试必须有地方能跑。

## 要解决的问题

`test_bwrap_daemon.py` 里 8 条 `requires_bwrap` 测试在 Windows 上全 skip，
而生产沙箱镜像**不带 pytest**（正确：生产容器不跑测试，且它能执行不可信代码，
不该因此多装一份依赖）。

结果就是这 8 条测试**在任何环境都不会被执行**——等于没有。
真实的隐患是 `_Daemon.kill()` 这类只在 Linux 上有意义的逻辑（进程组、
PID namespace），恰恰是 Windows 测不到的部分。

## 方案：多阶段构建里的独立 target

```
base       → 现有全部内容（= 生产镜像的能力）
test       → base + pytest
production → 空 stage，引用 base，**必须是文件里最后一个**
```

三条硬约束（本文件的测试逐条钉死）：

1. **test stage 装 pytest** —— 否则等于没有 target
2. **production stage 不许有 pytest** —— 生产镜像必须保持干净
3. **production 必须是最后一个 stage** —— `docker build` / `compose build`
   不指定 `--target` 时构建**最后一个** stage。若 test 在最后，
   一次普通的 `docker compose build sandbox` 就会把 pytest 打进生产镜像。

第 3 条是最容易犯且最难发现的：构建日志正常、镜像能起、
只有 `pip list` 里多一行。所以用静态测试钉住，不靠人记得。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SANDBOX_DOCKERFILE = BACKEND / "sandbox_service" / "Dockerfile"
COMPOSE = BACKEND / "deploy" / "docker-compose.yml"

# `FROM <image> AS <name>`（大小写不敏感，容忍多余空白）
_FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)(?:\s+AS\s+(?P<name>\S+))?\s*$", re.I)


def docker_stages(text: str) -> list[tuple[str, str, list[str]]]:
    """把 Dockerfile 切成 [(stage 名, 基础镜像, 该阶段的非注释行)]。

    无名 stage 用 `_stage_<序号>` 占位（Docker 允许无名 stage，
    现有文件就是无名 stage，不能因此漏掉它）。
    """
    stages: list[tuple[str, str, list[str]]] = []
    current: tuple[str, str, list[str]] | None = None
    for raw in text.splitlines():
        m = _FROM_RE.match(raw)
        if m:
            name = m.group("name") or f"_stage_{len(stages)}"
            current = (name, m.group("image"), [])
            stages.append(current)
            continue
        s = raw.strip()
        if current is not None and s and not s.startswith("#"):
            current[2].append(s)
    return stages


@pytest.fixture(scope="module")
def stages():
    assert SANDBOX_DOCKERFILE.exists(), f"找不到 {SANDBOX_DOCKERFILE}"
    return docker_stages(SANDBOX_DOCKERFILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stage_names(stages):
    return [s[0] for s in stages]


def _stage(stages, name: str) -> list[str]:
    for n, _base, lines in stages:
        if n == name:
            return lines
    raise AssertionError(f"没有名为 {name!r} 的 stage（实际：{[s[0] for s in stages]}）")


def _installs_pytest(lines: list[str]) -> bool:
    """该 stage 里是否有 pip install pytest（含 `pip install ... pytest`）。"""
    return any(
        "pytest" in line and ("pip install" in line or "pip3 install" in line)
        for line in lines
    )


# ---------------------------------------------------------------- 结构


def test_has_test_stage(stage_names):
    """必须有 `test` stage——没有就等于这 8 条测试无处可跑。"""
    assert "test" in stage_names, (
        f"Dockerfile 没有 test stage（实际 stage：{stage_names}）。"
        f"真 daemon 测试在 Windows 全 skip、生产镜像又没有 pytest，"
        f"于是这 8 条测试在任何环境都不会执行。"
    )


def test_has_production_stage(stage_names):
    """必须有显式的 `production` stage（生产镜像的落点要可指名）。"""
    assert "production" in stage_names, f"没有 production stage（实际：{stage_names}）"


def test_production_is_last_stage(stage_names):
    """★ 最关键：production 必须是**最后一个** stage。

    `docker build` 与 `docker compose build` 在不指定 `--target` 时构建
    **最后一个** stage。如果 test 在最后，那么一次普通的
    `docker compose build sandbox` 就会把 pytest 打进生产镜像。

    这条特别难在运行时发现：构建日志一切正常、服务能起、隔离也在，
    只有 `pip list` 里多一行 pytest。所以必须静态钉住。
    """
    assert stage_names[-1] == "production", (
        f"最后一个 stage 是 {stage_names[-1]!r}，不是 'production'。"
        f"不指定 --target 构建时会产出 {stage_names[-1]!r} 这个 stage —— "
        f"若它是 test，生产镜像会带上 pytest。"
    )


def test_test_stage_builds_from_service_stage(stages, stage_names):
    """test stage 必须基于服务镜像，而不是从零重装依赖。

    从 `python:3.12-slim` 重来会：① 构建慢几分钟 ② 装出来的依赖版本
    可能和生产不一致 —— 那样测的就不是生产实际跑的东西。
    """
    base_of_test = {n: b for n, b, _ in stages}["test"]
    assert base_of_test != "python:3.12-slim", (
        "test stage 直接基于 python:3.12-slim 会重装全部依赖，"
        "且版本可能与生产漂移——测的就不是生产镜像了"
    )
    assert base_of_test in stage_names or base_of_test.startswith("python:"), (
        f"test stage 的基础 {base_of_test!r} 既不是本文件里的 stage 也不是基础镜像"
    )


# ---------------------------------------------------------------- pytest 归属


def test_test_stage_installs_pytest(stages):
    """test stage 必须真的装 pytest。"""
    assert _installs_pytest(_stage(stages, "test")), (
        "test stage 没有安装 pytest"
    )


def test_production_stage_does_not_install_pytest(stages):
    """production stage 不许装 pytest。"""
    assert not _installs_pytest(_stage(stages, "production")), (
        "production stage 装了 pytest——生产镜像应保持干净"
    )


def test_base_stage_does_not_install_pytest(stages, stage_names):
    """base（服务本体）不许装 pytest，除非 production 显式覆盖。

    这是上面那条的加强版：pytest 如果加进 base，production 继承后一样脏。
    """
    base = "base" if "base" in stage_names else stage_names[0]
    assert not _installs_pytest(_stage(stages, base)), (
        f"stage {base!r} 装了 pytest，production 会继承这份依赖"
    )


def test_no_stray_pytest_install_outside_test_stage(stages):
    """除 test 外，任何 stage 都不许 pip install pytest。

    比逐个 stage 断言更彻底：将来加新 stage 也不会漏。
    """
    offenders = [
        name for name, _b, lines in stages
        if name != "test" and _installs_pytest(lines)
    ]
    assert not offenders, f"这些 stage 意外装了 pytest：{offenders}"


# ---------------------------------------------------------------- compose 侧


def test_compose_does_not_build_test_target():
    """生产 compose 不许把 sandbox 的构建 target 设成 test。

    设了就相当于「用测试镜像上线」：多一份 pytest、少一层有意为之的精简。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    live = [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for line in live:
        s = line.strip()
        if s.startswith("target:"):
            assert s.split(":", 1)[1].strip() != "test", (
                "compose 里把 build target 设成了 test——生产会用测试镜像"
            )
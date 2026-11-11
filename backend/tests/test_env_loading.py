"""验证 .env 加载：时机、优先级、健壮性。

背景（真实缺陷）：项目此前没有任何 .env 机制，所有 ``INNER_*`` / ``HLZ_*``
只能靠启动时 ``$env:FOO=...; python run.py`` 临时传入，重启终端即失效。

关键约束：多个模块在**导入时**就读取环境变量求值——
``app.quota.Limits`` 的字段默认值、``app.auth.SECRET`` 的模块级赋值、
``app.acp.config`` 的限额。因此 dotenv 必须早于任何子模块导入加载，
否则「配了不生效」，这是最容易踩的坑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent


def _run(code: str, env_extra: dict[str, str] | None = None) -> str:
    """在 backend/ 目录下跑一段独立 python，返回 stdout。"""
    import os

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("INNER_", "HLZ_", "GA_", "USER_MAX", "GLOBAL_MAX"))}
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND),
        capture_output=True, text=True, env=env, timeout=90,
    )
    out = (proc.stdout or "").strip()
    assert proc.returncode == 0, f"子进程失败：{out}\n{proc.stderr}"
    return out


def test_app_package_loads_without_env_file():
    """没有 .env 时必须正常导入，且不报错。"""
    out = _run("""
import sys
sys.path.insert(0, r'{backend}')
import app
print(app.dotenv_error)
""".format(backend=BACKEND))

    # 没有 .env 时 dotenv_loaded=False，但绝不能有 error
    assert out.strip() == "", f"无 .env 时不该报错，实际：{out}"


def test_env_file_values_reach_import_time_defaults():
    """核心用例：.env 里的值必须被「导入时求值」的模块吃到。

    ``Limits.user_code_runs`` 的默认值在类定义时就固化了，
    如果 dotenv 加载晚于 ``import app.quota``，这里会看到默认值 2。
    """
    env_file = ROOT / ".env"
    if env_file.exists():
        pytest.skip("仓库根已有真实 .env，避免覆盖")

    env_file.write_text("USER_MAX_CODE_RUNS=55\n", encoding="utf-8")
    try:
        out = _run("""
import sys
sys.path.insert(0, r'{backend}')
import app
from app.quota import Limits
print(Limits().user_code_runs)
""".format(backend=BACKEND))
        assert out == "55", f".env 未在导入时生效，实际拿到 {out}（默认值 2 = 加载太晚）"
    finally:
        env_file.unlink(missing_ok=True)


def test_real_env_overrides_dotenv():
    """真实环境变量必须胜过 .env（CI / 临时覆盖依赖这条）。"""
    env_file = ROOT / ".env"
    if env_file.exists():
        pytest.skip("仓库根已有真实 .env，避免覆盖")

    env_file.write_text("USER_MAX_CODE_RUNS=55\n", encoding="utf-8")
    try:
        out = _run(
            """
import sys
sys.path.insert(0, r'{backend}')
import app
from app.quota import Limits
print(Limits().user_code_runs)
""".format(backend=BACKEND),
            {"USER_MAX_CODE_RUNS": "99"},
        )
        assert out == "99", f"真实环境变量应胜出，实际 {out}"
    finally:
        env_file.unlink(missing_ok=True)


def test_acp_settings_read_from_dotenv():
    """ACP 的 INNER_ACP_* 由 Hlaudz 自己读取，也必须能走 .env。"""
    env_file = ROOT / ".env"
    if env_file.exists():
        pytest.skip("仓库根已有真实 .env，避免覆盖")

    env_file.write_text("INNER_ACP_MODE=read_only\n", encoding="utf-8")
    try:
        out = _run("""
import sys
sys.path.insert(0, r'{backend}')
import app
from app.acp.config import load_acp_settings
print(load_acp_settings().mode)
""".format(backend=BACKEND))
        assert out == "read_only", f"ACP 配置未从 .env 读取，实际 {out}"
    finally:
        env_file.unlink(missing_ok=True)


def test_malformed_env_file_does_not_crash_startup():
    """脏 .env 不能让服务起不来：加载失败也要能导入 app。"""
    env_file = ROOT / ".env"
    if env_file.exists():
        pytest.skip("仓库根已有真实 .env，避免覆盖")

    # 故意写一行格式异常的内容
    env_file.write_text("THIS IS NOT VALID = = =\n", encoding="utf-8")
    try:
        out = _run("""
import sys
sys.path.insert(0, r'{backend}')
import app
print('imported')
""".format(backend=BACKEND))
        assert "imported" in out, f"脏 .env 阻断了启动：{out}"
    finally:
        env_file.unlink(missing_ok=True)


def test_requirements_pins_python_dotenv():
    """依赖必须钉住，否则新环境装不上。"""
    req = (BACKEND / "requirements.txt").read_text(encoding="utf-8")

    assert "python-dotenv==" in req, "requirements.txt 缺少 python-dotenv"


def test_gitignore_excludes_env_but_keeps_example():
    """安全：.env 绝不能入库，但模板要入库。"""
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".env" in gi, ".gitignore 必须排除 .env（含密钥）"
    assert "!.env.example" in gi, ".env.example 是模板，必须显式允许入库"
    assert (ROOT / ".env.example").is_file(), ".env.example 模板文件必须存在"
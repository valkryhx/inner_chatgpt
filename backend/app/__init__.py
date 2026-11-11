"""Hlaudz 后端包。

**这里必须最先加载 .env**，不得挪到别处。

原因：多个模块在**导入时**就读取环境变量求值——

- ``app.quota.Limits`` 的字段默认值是 ``_env_int(...)``，类定义时即固化；
- ``app.auth.SECRET`` 是模块级 ``os.environ.get(...)``；
- ``app.acp.config`` 的限流/超时同理。

因此 ``load_dotenv()`` 必须早于任何子模块导入。包 ``__init__`` 是唯一能保证
这一点的位置（导入任一子模块都会先执行父包 ``__init__``）。

优先级：**真实环境变量 > .env**（``override=False``）。这样 CI / 临时覆盖
（``$env:FOO=...; python run.py``）始终生效，.env 只作为默认值来源。
"""
from __future__ import annotations

from pathlib import Path

# .env 在仓库根目录（与 README、docs 同级），而不是 backend/ 下。
_ROOT_DIR = Path(__file__).resolve().parents[2]
_ENV_FILE = _ROOT_DIR / ".env"

dotenv_loaded = False
dotenv_error = ""


def _load_dotenv() -> None:
    """加载根目录 .env；缺失或未装 python-dotenv 都静默跳过，绝不阻断启动。"""
    global dotenv_loaded, dotenv_error
    if not _ENV_FILE.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError as exc:  # 未装依赖时 .env 不生效，但不该阻止服务启动
        dotenv_error = f"python-dotenv 未安装：{exc}"
        return
    try:
        dotenv_loaded = bool(load_dotenv(_ENV_FILE, override=False))
    except Exception as exc:  # noqa: BLE001 - 脏 .env 不能拖垮启动
        dotenv_error = f"{type(exc).__name__}: {exc}"


_load_dotenv()
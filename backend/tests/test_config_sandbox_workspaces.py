"""`app.config.SANDBOX_WORKSPACE_USERS` 的环境变量解析。

它是**模块导入时求值**的常量（不是函数），所以不能靠 monkeypatch 测，
必须起子进程、设环境变量、再导入。

锁两件事：
1. 未设置时 = 默认 2 人白名单；
2. **显式空串 = 全部用户**（不能退化成默认值）。

第 2 条是本项目已踩过的坑：`os.environ.get(k) or default` 会把空串吞掉，
导致「关掉白名单」这个配置静默失效。`sandbox/config.py` 已修，
`app/config.py` 必须有同样语义，否则两处路由不一致。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

SNIPPET = (
    "from app.config import SANDBOX_WORKSPACE_USERS as u;"
    "print('|'.join(u))"
)


def run_with(env_value: str | None) -> str:
    env = dict(os.environ)
    env.pop("HLZ_SANDBOX_MVP_USERS", None)
    if env_value is not None:
        env["HLZ_SANDBOX_MVP_USERS"] = env_value
    r = subprocess.run([sys.executable, "-c", SNIPPET], cwd=BACKEND,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, f"子进程失败：{r.stderr[-800:]}"
    return r.stdout.strip()


def test_default_when_unset():
    out = run_with(None)
    assert out == "u_c56b8337dd09|u_343a46896bff"


def test_explicit_empty_means_everyone():
    """显式空串必须解析成空元组（= 不分用户，全部走沙箱根）。"""
    out = run_with("")
    assert out == "", f"空串被吞成默认值了：{out!r}"


def test_whitespace_and_blanks_are_stripped():
    out = run_with(" u_a , u_b ,, u_c ")
    assert out == "u_a|u_b|u_c", out
"""开发启动入口：python run.py

**`.env` 在这里先加载一次**，原因见 ``app/__init__.py`` 的模块说明：
多个模块在导入时就读取环境变量（``app.quota.Limits`` / ``app.auth.SECRET``），
必须早于 uvicorn 通过字符串导入 ``app.main:app``。

``app/__init__.py`` 里也有一份加载（用于 pytest / ``uvicorn app.main:app``
等不经本文件的入口）。两处都是幂等的：``load_dotenv(override=False)``
不会覆盖已存在的环境变量。
"""
from __future__ import annotations

from pathlib import Path

# 必须先于 `import uvicorn` 之后的任何 app 模块导入。
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if _ENV_FILE.is_file():
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_FILE, override=False)
    except Exception:  # noqa: BLE001 - 脏 .env / 未装依赖都不该阻止启动
        pass

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
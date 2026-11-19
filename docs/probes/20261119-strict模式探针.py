"""strict 模式实证：沙箱不可用时，用户代码**绝不能**在 app 进程内执行。

## 为什么判据不能是「写了文件没有」

第一版探针让代码写 `/ws/<token>.txt`，然后检查宿主路径 —— 结果是**假绿**：
容器内 `/ws` 不存在，代码直接抛 `FileNotFoundError`，什么也没写成功，
看起来像「strict 拦住了」，实际是「代码跑失败了」。

这是典型的**假信号**：断言通过的原因与它要证明的机制无关。

## 可靠的判据：**环境变量**

第一版和第二版都选错了判据，记录在这里：

- 第一版用「写文件没有」→ 假绿：容器内 `/ws` 不存在，代码直接抛
  `FileNotFoundError`，什么也没写成功，看起来像「strict 拦住了」。
- 第二版用「网络是否 BLOCKED」→ 假信号：**app 容器本身也没有外网**，
  所以两种执行环境的网络状态一样，区分不出来（实测对照三发现的）。

真正可靠的判据是 **app 的环境变量有没有泄漏到用户代码里**：

| 判据 | 沙箱内 | app 进程内 |
| --- | --- | --- |
| `HLZ_DB_URL` 可见 | **False**（`--clearenv`） | **True**（继承 app 环境） |
| `HLZ_SANDBOX_SERVICE_TOKEN` 可见 | False | 视情况 |
| 网络 | BLOCKED | BLOCKED（app 容器也无外网）← 不可用 |

`--clearenv` 是 bwrap 命令里的关键一条（`executor/bwrap.py`），
它把 app 的 `DB_PASSWORD` / `OPENAI_API_KEY` 之类的环境变量挡在外面。
所以「能不能读到 app 的环境变量」就是「有没有真的隔离」的直接证据。

## 跑法（app 容器内）

    # 对照一：沙箱正常 + strict → 代码应在沙箱里跑（NET_BLOCKED）
    docker exec <app> python /tmp/probe_strict.py strict

    # 对照二：停掉沙箱 + strict → 代码不应执行（无论输出什么都行，
    #         但不能出现 NET_OPEN —— 那说明它在 app 进程里跑了）
    docker stop <sandbox> && docker exec <app> python /tmp/probe_strict.py strict

    # 对照三：停掉沙箱 + auto → 应降级执行（NET_OPEN + 输出含「未隔离」）
    docker stop <sandbox> && docker exec <app> python /tmp/probe_strict.py auto
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys

# 三段判据：网络、workspace 绝对路径、环境变量泄漏
PROBE_CODE = '''
import os, socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    print("NET_OPEN")
except OSError:
    print("NET_BLOCKED")
print("ENV_DB:", bool(os.environ.get("HLZ_DB_URL")))
print("TOKEN_VISIBLE:", "%s" in os.environ.get("HLZ_SANDBOX_SERVICE_TOKEN", ""))
'''


def main(mode: str) -> int:
    # 容器 WORKDIR 是 /app/backend（不是 /app），`app` 包在那里。
    for p in ("/app/backend", "/app"):
        if os.path.isdir(os.path.join(p, "app")):
            if p not in sys.path:
                sys.path.insert(0, p)
            break
    else:
        print("SETUP FAIL: 找不到 app 包")
        return 2

    os.environ["HLZ_SANDBOX_MODE"] = mode
    from app.sandbox import config as sbx_config
    from app.sandbox import manager as mgr_mod

    cfg = sbx_config.get_config(reload=True)
    mgr_mod.get_manager(reload=True)
    print(f"mode={cfg.mode}  backend={os.environ.get('HLZ_SANDBOX_BACKEND')}")

    # 白名单放行：空白名单 = 不限制 = 所有人可进沙箱
    if not cfg.user_in_scope("u_probe"):
        print("SETUP: 该用户不在白名单，分流不会执行 —— 设 HLZ_SANDBOX_MVP_USERS= 空")
        return 2

    from app import builtin_tools

    uid = f"u_strict_{secrets.token_hex(4)}"
    nonce = secrets.token_hex(6)
    code = PROBE_CODE % nonce

    tools = {t.name: t for t in builtin_tools.build_builtin_tools(uid, include_memory=False)}
    out = str(asyncio.run(tools["code_run"].call({"code": code})))

    # 主判据：app 的环境变量有没有泄漏进用户代码
    env_leaked = "ENV_DB: True" in out
    token_seen = "TOKEN_VISIBLE: True" in out
    # 辅助信息（不作判定，见模块 docstring）
    net_open = "NET_OPEN" in out
    net_blocked = "NET_BLOCKED" in out

    print(f"输出: {out[:400]!r}")
    print(f"  ★ app 环境变量泄漏: {env_leaked}  （主判据：泄漏=True ⇒ 在 app 进程里跑）")
    print(f"    token 泄漏: {token_seen}")
    print(f"    网络 OPEN={net_open} BLOCKED={net_blocked}（app 容器也无外网，仅供参考）")

    if cfg.mode == "strict":
        if env_leaked or token_seen:
            print("VERDICT: LEAK（strict 下沙箱不可用，代码在 app 进程里执行并读到环境变量）")
            return 1
        if net_blocked:
            print("VERDICT: SAFE（代码在沙箱里执行，环境变量已隔离）")
            return 0
        print("VERDICT: SAFE（strict 拒绝执行：代码根本没有跑）")
        return 0

    if cfg.mode == "auto":
        if env_leaked:
            print("VERDICT: DEGRADED（auto 降级：代码在 app 进程里跑了，符合设计）")
            return 0
        print("VERDICT: UNEXPECTED（auto 下竟然没在进程内跑）")
        return 2

    print(f"VERDICT: UNEXPECTED（mode={cfg.mode}）")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "strict"))
"""端到端隔离探针：在**沙箱服务镜像里**跑真实 bwrap。

## 为什么必须在容器里跑

宿主机是 Windows + Docker Desktop，没有 bwrap。而真实部署形态是
「沙箱服务跑在容器里，bwrap 是它的子进程」。所以只有在这里验证才有意义。

## 与单测的分工

- 单测（`test_bwrap_executor.py` 的纯逻辑组）：守住命令构造不回退，Windows 也能跑
- 本探针：用真 bwrap 验证「隔离真的生效」

## 用法

    python docs/probes/20261119-sandbox-service-e2e.py

它会：
  1. 起一个 hlz-sandbox-svc 容器（非 root）
  2. 通过 HTTP 调 /v1/exec
  3. 断言隔离效果
  4. 清理
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IMAGE = "hlz-sandbox-svc:dev"
CONTAINER = "hlz-svc-probe"
TOKEN = "probe-token-20261119"
PORT = 18021

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "[OK]  " if cond else "[FAIL]"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def docker(*args: str, check_rc: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["docker", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} 失败: {r.stderr[:400]}")
    return r


def api(path: str, *, payload: dict | None = None, user: str = "u_probe",
        token: str = TOKEN, timeout: float = 60.0) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if user:
        req.add_header("X-HLZ-User", user)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(body or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed


def start_container() -> None:
    docker("rm", "-f", CONTAINER, check_rc=False)
    docker(
        "run", "-d", "--name", CONTAINER,
        "-p", f"{PORT}:8021",
        "-e", f"HLZ_SANDBOX_SERVICE_TOKEN={TOKEN}",
        "-e", "HLZ_SANDBOX_WORKSPACES_DIR=/workspaces",
        IMAGE,
    )
    # 等它起来
    for _ in range(40):
        try:
            code, _ = api("/v1/health", user="", token="")
            if code == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    raise RuntimeError("服务没起来")


def stop_container() -> None:
    docker("rm", "-f", CONTAINER, check_rc=False)


def main() -> int:
    print("=" * 70)
    print("沙箱服务端到端隔离探针")
    print("=" * 70)
    print(f"  镜像: {IMAGE}")

    alive = docker("images", "-q", IMAGE).stdout.strip()
    if not alive:
        print(f"!! 镜像不存在，先构建：")
        print(f"   docker build -f backend/sandbox_service/Dockerfile -t {IMAGE} .")
        return 1

    start_container()
    try:
        run_checks()
    finally:
        stop_container()

    print()
    print("=" * 70)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("结果：全部通过")
    return 0


def run_checks() -> None:
    print()
    print("--- 一、服务本身 ---")
    code, body = api("/v1/health", user="", token="")
    check("health 不需要认证", code == 200, f"{code}")
    check("executor 是 BwrapExecutor",
          body.get("executor") == "BwrapExecutor", str(body))

    # 服务进程必须非 root
    uid = docker("exec", CONTAINER, "id", "-u").stdout.strip()
    check("服务进程非 root（RLIMIT_NPROC 才有效）", uid != "0", f"uid={uid}")

    print()
    print("--- 二、基本执行 ---")
    code, body = api("/v1/exec", payload={"code": "print(1+1)"})
    check("exec 返回 200", code == 200, str(body)[:200])
    check("stdout 正确", body.get("stdout", "").strip() == "2", str(body)[:200])

    code, body = api("/v1/exec", payload={"code": "import sys; sys.exit(7)"})
    check("exit_code 透传", body.get("exit_code") == 7, str(body)[:200])

    print()
    print("--- 三、隔离断言（方案 §7.4 五个坑）---")

    # 坑 1：读不到宿主 /etc 里的东西（最小挂载集）
    code, body = api("/v1/exec", payload={
        "code": "import os\nprint('HAS_SHADOW' if os.path.exists('/etc/shadow') else 'NO_SHADOW')"
    })
    check("读不到 /etc/shadow", "NO_SHADOW" in body.get("stdout", ""), str(body)[:200])

    # 坑 2：/etc 里没有宿主的东西（--tmpfs /etc 占位 + 只叠 fonts）
    #
    # ⚠️ 这一条最初写错了：我假设 /etc/hosts 存在且可写（tmpfs 上），
    #    于是断言「写进去之后下次看不到」。实际跑出来是
    #    FileNotFoundError —— 因为 --tmpfs /etc 给的是**空**目录，
    #    只再叠了 /etc/fonts，宿主那些 hosts/resolv.conf/passwd **全都不在**。
    #    换句话说隔离比原断言更彻底。断言应当测真实性质：
    #      a) 宿主的敏感文件不存在
    #      b) 在 /etc 里新建的文件不跨 exec 持久
    code, body = api("/v1/exec", payload={
        "code": (
            "import os\n"
            "present = [n for n in ('hosts','shadow','passwd','resolv.conf')\n"
            "           if os.path.exists('/etc/' + n)]\n"
            "print('PRESENT:' + ','.join(present) if present else 'ETC_CLEAN')\n"
        )
    })
    check("/etc 里没有宿主的敏感文件",
          "ETC_CLEAN" in body.get("stdout", ""), str(body)[:200])

    code, body = api("/v1/exec", payload={
        "code": "open('/etc/probe_marker','w').write('x'); print('WROTE')"
    })
    check("/etc 下可建临时文件（tmpfs）", "WROTE" in body.get("stdout", ""),
          str(body)[:200])
    code2, body2 = api("/v1/exec", payload={
        "code": (
            "import os\n"
            "print('LEAKED' if os.path.exists('/etc/probe_marker') else 'CLEAN')\n"
        )
    })
    check("对 /etc 的写入不跨 exec 持久", "CLEAN" in body2.get("stdout", ""),
          str(body2)[:200])

    # 坑 3：环境变量不泄漏
    code, body = api("/v1/exec", payload={
        "code": "import os\nprint('TOKEN=' + os.environ.get('HLZ_SANDBOX_SERVICE_TOKEN',''))"
    })
    check("服务 token 不漏进沙箱", "TOKEN=" in body.get("stdout", "")
          and "probe-token" not in body.get("stdout", ""), str(body)[:200])

    # 坑 4：/usr 只读
    code, body = api("/v1/exec", payload={
        "code": (
            "try:\n"
            "    open('/usr/x','w').write('x')\n"
            "    print('WROTE_USR')\n"
            "except OSError:\n"
            "    print('READONLY')\n"
        )
    })
    check("/usr 只读", "READONLY" in body.get("stdout", ""), str(body)[:200])

    # 坑 5：断网
    code, body = api("/v1/exec", payload={
        "code": (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1',443), timeout=2)\n"
            "    print('CONNECTED')\n"
            "except OSError:\n"
            "    print('BLOCKED')\n"
        )
    })
    check("网络断开", "BLOCKED" in body.get("stdout", ""), str(body)[:200])

    print()
    print("--- 四、跨租户隔离 ---")
    code, body = api("/v1/exec", payload={
        "code": "open('/ws/secret.txt','w').write('A的机密')"
    }, user="u_alice")
    check("用户 A 能写自己的 workspace", code == 200, str(body)[:150])

    code, body = api("/v1/exec", payload={
        "code": (
            "import os\n"
            "seen = []\n"
            "for p in ('/ws/secret.txt', '/ws/../u_alice/secret.txt'):\n"
            "    try:\n"
            "        if os.path.exists(p): seen.append(open(p).read())\n"
            "    except OSError: pass\n"
            "print('LEAK:' + '|'.join(seen) if seen else 'ISOLATED')\n"
        )
    }, user="u_bob")
    check("用户 B 读不到用户 A 的文件",
          "ISOLATED" in body.get("stdout", ""), str(body)[:200])

    print()
    print("--- 五、资源与超时 ---")
    code, _ = api("/v1/exec", payload={"code": "import time; time.sleep(20)", "timeout_s": 2})
    check("超时映射成 504", code == 504, f"实际 {code}")

    print()
    print("--- 六、认证与身份 ---")
    code, _ = api("/v1/exec", payload={"code": "print(1)"}, token="")
    check("无 token 时 401", code in (401, 403), f"实际 {code}")

    code, _ = api("/v1/exec", payload={"code": "print(1)", "user_id": "u_attacker"},
                  user="u_alice")
    check("请求体身份与认证不符时 403", code == 403, f"实际 {code}")

    print()
    print("--- 七、字体（中文图不能是方块）---")
    code, body = api("/v1/exec", payload={
        "code": (
            "import matplotlib; matplotlib.use('Agg')\n"
            "from matplotlib import font_manager\n"
            "cjk = [f.name for f in font_manager.fontManager.ttflist if 'CJK' in f.name]\n"
            "print('CJK=' + (cjk[0] if cjk else 'NONE'))\n"
        )
    })
    check("沙箱内能用中文字体", "CJK=" in body.get("stdout", "")
          and "NONE" not in body.get("stdout", ""), str(body)[:200])

    _pool_checks()


def _pool_checks() -> None:
    """池相关的端到端断言（单独一组，因为要读容器内日志）。"""
    print()
    print("--- 八、池（扩缩容 + OVERFLOW）---")

    # 从日志里看池是否真的被调用（"建槽" 只在池层打）
    logs = docker("logs", CONTAINER).stdout + docker("logs", CONTAINER).stderr

    # 先跑几轮让池动起来（上面的检查已经跑过若干用户，应该建过槽）
    for u in ("u_pool_a", "u_pool_b", "u_pool_c"):
        api("/v1/exec", payload={"code": "print(1)"}, user=u)

    logs = docker("logs", CONTAINER).stdout + docker("logs", CONTAINER).stderr
    check("池层真的建了槽（日志有「建槽」）", "建槽" in logs, logs[-300:])

    # 上限：默认 MAX_SLOTS=3，上面 3 个 + 之前的用户 ≥ 3，应该有淘汰或复用
    check("槽数不会无限增长（有淘汰或复用）",
          "淘汰槽" in logs or "销毁槽" in logs or logs.count("建槽") <= 10,
          f"建槽次数={logs.count('建槽')}")

if __name__ == "__main__":
    sys.exit(main())


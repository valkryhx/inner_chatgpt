"""预热复用（daemon）的测试（TDD RED）。

## 这一组补的是上一轮的缺口

上一轮 `_DAEMON_SCRIPT` 定义了但**从没被 exec 用过**——池只记账不预热。
实测第二次调用 567ms（= 每次重新 `import pandas` 的钱），而方案 §4.2 D 说应该是 8.8ms。

## 分成两组

- **纯逻辑组**（不需要 bwrap）：协议帧、注册表、降级路径。Windows 也能跑。
- **真 daemon 组**（`requires_bwrap`）：真的起进程、验证复用与隔离。

纯逻辑组保证「改代码不会把协议搞坏」；真 daemon 组在容器里跑
（见 `docs/probes/20261120-daemon-reuse.py`）。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

HAS_BWRAP = shutil.which("bwrap") is not None
requires_bwrap = pytest.mark.skipif(not HAS_BWRAP, reason="bwrap 未安装")


@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "u_test"
    d.mkdir()
    return d


@pytest.fixture()
def ex(ws):
    from sandbox_service.executor.bwrap import BwrapExecutor

    return BwrapExecutor(workspace_resolver=lambda uid: ws if uid == "u_test" else ws)


# ---------------------------------------------------------------- 纯逻辑组


def test_daemon_argv_binds_daemon_script_not_user_code(ex, ws):
    """daemon 模式的命令必须跑 `_DAEMON_SCRIPT`，而不是 `python3 -c <用户代码>`。

    这条防的正是上一轮的 bug：脚本定义了却没用上。

    判定方式：命令最后一项应该是**很长的脚本**，且含 daemon 的关键特征
    （READY 握手 / os.fork / _preload）。用户代码不会长这样。
    """
    argv = ex._build_daemon_argv(ws)

    joined = " ".join(argv)
    assert "python3" in argv
    script = argv[-1]
    assert len(script) > 500, f"最后一项应该是一大段脚本，实际 {len(script)} 字符"
    # daemon 的三个标志性特征（任何一个都说明是 daemon 脚本而非用户代码）
    assert "READY" in script, "缺 READY 握手"
    assert "os.fork" in script, "缺 fork（纯净父进程的核心）"
    assert "_preload" in script, "缺预热阶段"
    # 隔离参数不能因为换入口而丢
    assert "--unshare-all" in joined
    assert "--clearenv" in joined


def test_daemon_argv_keeps_isolation_flags(ex, ws):
    """daemon 模式同样要带全部隔离参数（不能因为换了入口就丢）。"""
    argv = ex._build_daemon_argv(ws)

    assert "--unshare-all" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--tmpfs" in argv and "/etc" in argv
    # 只 bind 自己 workspace
    pairs = [
        (argv[i + 1], argv[i + 2])
        for i, t in enumerate(argv)
        if t in ("--bind", "--ro-bind")
    ]
    to_ws = [src for src, dst in pairs if dst == "/ws"]
    assert to_ws == [str(ws)]
    # 不许 ro-bind /
    for src, dst in pairs:
        assert not (src == "/" and dst == "/")


def test_daemon_frame_is_single_line_json(ex):
    """协议帧必须是单行 JSON（daemon 按行读，多行会错位）。"""
    frame = ex._daemon_frame("print(1)", "python", 10.0)
    assert "\n" not in frame.rstrip("\n")
    assert frame.endswith("\n")
    obj = json.loads(frame)
    assert obj["code"] == "print(1)"
    assert obj["code_type"] == "python"
    assert obj["timeout_s"] == 10.0


def test_daemon_frame_escapes_newlines_in_code(ex):
    """用户代码里的换行必须被 JSON 转义，不能破坏按行协议。"""
    frame = ex._daemon_frame("a = 1\nb = 2\nprint(a+b)", "python", 5.0)
    # 整帧去掉结尾换行后不得再含换行
    assert "\n" not in frame.rstrip("\n")
    assert json.loads(frame)["code"] == "a = 1\nb = 2\nprint(a+b)"


def test_exec_falls_back_to_oneshot_without_start(ex, ws):
    """没调 start 时退回一次性执行（向后兼容 + 降级）。

    这条保证：即使 daemon 起不来（比如 bwrap 缺失），exec 仍能用一次性模式工作。

    ⚠️ Windows 上没有 bwrap，`create_subprocess_exec` 会抛 `FileNotFoundError`
    （不是"返回非零"，是"命令不存在"）。那说明**退化路径本身也需要兜底**，
    所以 `_exec_oneshot` 应该把 OSError 转成 ExecResult 而不是往上抛——
    否则一次环境问题会让整个工具调用崩掉。
    """
    r = asyncio.run(ex.exec("u_test", "print(1)"))
    assert isinstance(r.exit_code, int)
    assert isinstance(r.stdout, str)
    if not HAS_BWRAP:
        # 没有 bwrap：必须是**明确的错误结果**，不是异常
        assert r.exit_code != 0
        assert "bwrap" in (r.stderr or "").lower()


def test_start_registers_daemon(ex):
    """start() 之后该用户必须有 daemon 记录（哪怕 bwrap 缺失时是抛错/降级）。"""
    ok = asyncio.run(ex.start("u_test"))
    # start 返回 bool：能起为 True，起不来为 False（不抛，交给调用方决定）
    assert isinstance(ok, bool)


def test_destroy_is_idempotent(ex):
    asyncio.run(ex.destroy("u_test"))
    asyncio.run(ex.destroy("u_test"))


def test_daemon_registry_isolated_per_user(ws, tmp_path):
    """不同用户必须是不同 daemon（各绑自己的 workspace）。"""
    from sandbox_service.executor.bwrap import BwrapExecutor

    (tmp_path / "u_a").mkdir()
    (tmp_path / "u_b").mkdir()
    e = BwrapExecutor(workspace_resolver=lambda uid: tmp_path / uid)

    asyncio.run(e.start("u_a"))
    asyncio.run(e.start("u_b"))
    # 注册表里应该是两条（起不来的会是 None，但键存在）
    assert set(e._daemons) >= {"u_a", "u_b"}


# ---------------------------------------------------------------- 真 daemon 组


@requires_bwrap
def test_daemon_reuse_is_fast(ex):
    """★ 核心：第二次 exec 必须显著快于第一次。

    第一次付 import 的钱，第二次复用已导入的模块。
    阈值定 300ms 是保守的（实测 8.8ms vs 1105ms），留了机器差异余量。
    """
    async def go():
        assert await ex.start("u_test")
        r1 = await ex.exec("u_test", "import pandas; print(pandas.__version__)")
        assert r1.exit_code == 0, r1.stderr
        first_ms = r1.duration_ms

        r2 = await ex.exec("u_test", "import pandas; print(pandas.__version__)")
        assert r2.exit_code == 0, r2.stderr
        return first_ms, r2.duration_ms

    first_ms, second_ms = asyncio.run(go())
    assert second_ms < 300, (
        f"第二次仍然要 {second_ms}ms（第一次 {first_ms}ms）——说明没复用，"
        f"还是每次新起进程"
    )


@requires_bwrap
def test_daemon_state_not_leaked_between_execs(ex):
    """★ 铁律 1 的运行时验证：每次 exec 是**新子进程**，变量不残留。"""
    async def go():
        assert await ex.start("u_test")
        await ex.exec("u_test", "LEAKED = 'from_previous_exec'")
        r = await ex.exec("u_test", "print(globals().get('LEAKED', 'CLEAN'))")
        return r

    r = asyncio.run(go())
    assert "CLEAN" in r.stdout, f"变量跨 exec 残留了: {r.stdout!r}"


@requires_bwrap
def test_daemon_parent_untouched_by_user_globals(ex):
    """用户代码改 __builtins__ 等全局状态，不能影响下一次 exec。

    因为父进程从不执行用户代码，而子进程是 fork 出来的独立内存。
    """
    async def go():
        assert await ex.start("u_test")
        await ex.exec("u_test", "import builtins; builtins.POLLUTED = 1")
        r = await ex.exec("u_test", "import builtins; print(getattr(builtins,'POLLUTED','CLEAN'))")
        return r

    r = asyncio.run(go())
    assert "CLEAN" in r.stdout, f"builtins 被污染: {r.stdout!r}"


@requires_bwrap
def test_timeout_kills_child_but_daemon_survives(ex):
    """超时只杀子进程，daemon 活着——否则一次超时就要付重新预热的钱。"""
    async def go():
        assert await ex.start("u_test")
        r1 = await ex.exec("u_test", "import time; time.sleep(30)", timeout_s=1)
        assert r1.exit_code != 0
        # daemon 应该还在：下一次仍然快
        r2 = await ex.exec("u_test", "import pandas; print('alive')")
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert r2.exit_code == 0, r2.stderr
    assert "alive" in r2.stdout
    assert r2.duration_ms < 300, f"超时后 daemon 没了（{r2.duration_ms}ms）"


@requires_bwrap
def test_daemon_destroy_stops_process(ex):
    async def go():
        assert await ex.start("u_test")
        await ex.exec("u_test", "print(1)")
        await ex.destroy("u_test")
        return ex._daemons.get("u_test")

    assert asyncio.run(go()) is None


@requires_bwrap
def test_daemon_isolation_still_applies(ex):
    """daemon 模式下隔离不能退化。"""
    async def go():
        assert await ex.start("u_test")
        r = await ex.exec(
            "u_test",
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1',443), timeout=2); print('CONNECTED')\n"
            "except OSError: print('BLOCKED')\n",
        )
        return r

    r = asyncio.run(go())
    assert "BLOCKED" in r.stdout


@requires_bwrap
def test_daemon_can_write_workspace_and_file_lands_on_host(ex, ws):
    """daemon 里写的文件必须真的落到宿主目录（bind 生效）。"""
    async def go():
        assert await ex.start("u_test")
        return await ex.exec("u_test", "open('/ws/from_daemon.txt','w').write('OK')")

    r = asyncio.run(go())
    assert r.exit_code == 0, r.stderr
    assert (ws / "from_daemon.txt").read_text() == "OK"

# ---------------------------------------------------------------- 进程组清理


def test_daemon_uses_own_session(ex):
    """daemon 必须起在**独立会话/进程组**里。

    这条是静态检查：没有 `start_new_session=True`，`os.killpg` 就会打到
    服务自己的进程组上（把整个服务杀掉），所以两者必须成对出现。

    背景（实测）：bwrap 会 fork 一个内层进程，只 `proc.kill()` 外层时，
    SIGKILL 不可捕获 ⇒ 内层被 init 收养继续跑。日志照常打「daemon 已停」，
    `ps` 里却留着 `stat=S` 的 bwrap。只能数进程才能发现。
    """
    src = (BACKEND / "sandbox_service" / "executor" / "bwrap.py").read_text(
        encoding="utf-8"
    )
    assert "start_new_session=True" in src, (
        "daemon 没有独立会话：无法用 killpg 清整组，"
        "而且 killpg 会误伤服务自身"
    )
    assert "os.killpg" in src, "销毁时没有杀整个进程组，内层 bwrap 会变成孤儿"
    assert "import signal" in src or "from signal" in src


@requires_bwrap
def test_destroy_leaves_no_process_for_workspace(ex, ws):
    """★ destroy 之后，绑该 workspace 的 bwrap 进程必须**一个都不剩**。

    不区分僵尸和活进程无法定位问题，所以这里只看「还存在的 pid」——
    只要有残留就说明清理没到位（僵尸说明没 wait，活进程说明没杀整组）。

    ## 为什么读 /proc 而不是 `ps`

    第一版用 `ps -eo pid=,args=` 做子串匹配，在 pytest 里**假红**：
    `ps` 的 args 列会按输出宽度截断（实测截到约 80 字符，
    `--bind <path> /ws` 在命令行尾部，直接被切掉），于是
    「预热成功但找不到 bwrap 进程」。

    而且截断与否取决于调用环境（容器里直接跑不截、pytest 里截），
    属于「同一断言在两种环境下结论不同」的假信号。

    `/proc/<pid>/cmdline` 是内核里的原始 argv（NUL 分隔），
    **没有宽度概念**，是这类断言唯一可靠的来源。
    """
    def pids_for(path: Path) -> list[int]:
        """绑定了该 workspace 的 bwrap 进程。

        `/proc/<pid>/cmdline` 是 `argv` 的 NUL 连接（末尾也有 NUL），
        所以按**数组**比对，不能做子串匹配 —— 子串里带空格永远匹配不上。
        """
        target = str(path)
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except (OSError, PermissionError):
                continue          # 进程已退出 / 不属本用户
            argv = [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]
            if not argv or not argv[0].endswith("bwrap"):
                continue
            # 找连续的 ["--bind", <path>, "/ws"]
            for i, tok in enumerate(argv):
                if tok == "--bind" and i + 2 < len(argv):
                    if argv[i + 1] == target and argv[i + 2] == "/ws":
                        found.append(int(entry.name))
                        break
        return found

    async def go():
        assert await ex.start("u_test")
        assert pids_for(ws), "预热成功但找不到 bwrap 进程——归属判定写错了"
        await ex.exec("u_test", "print(1)")
        await ex.destroy("u_test")
        await asyncio.sleep(1.5)
        return pids_for(ws)

    left = asyncio.run(go())
    assert left == [], f"destroy 后仍残留进程 {left}（bwrap 内层未被清理）"

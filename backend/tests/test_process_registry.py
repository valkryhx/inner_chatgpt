import asyncio
import os
import subprocess

import pytest

from app import process_registry
from app.builtin_tools import _run_code_impl


def run(coro):
    return asyncio.run(coro)


def pid_alive(pid: int) -> bool:
    """这个 pid 对应的进程是否还活着。

    ⚠️ **Windows 上不用 `os.kill(pid, 0)` 判活**，因为它会**假阳**。

    实测（`tools/turn_job_verify/pid_alive_check.py`）：在全量测试跑到这条
    用例失败的那一刻抓现场，同一个 pid 上三个判据是

        os.kill(pid, 0)      → 判「活」
        tasklist /FI PID eq  → 判「死」
        psutil.pid_exists    → 判「死」

    50/50 轮全部不一致 —— 进程**真的死了**，是 `os.kill` 谎报。原因是 Windows
    的进程对象只要系统里还有人持句柄就继续存在，而 `os.kill` 走的是 OpenProcess，
    不是「是否在运行」；进程退出后句柄何时全部释放不受我们控制。

    注意别把它说成「`os.kill` 恒误判」：在真实 `_run_code_impl` 路径下单独跑时
    它通常**正确**报死（`OSError winerror=87`，8/8 实测），所以单独跑 20/20 通过；
    只有句柄恰好还活着的时候才假阳 —— 这也是这条用例「单跑绿、全量偶发红」的原因。

    所以 Windows 用 tasklist（系统自带，无需新依赖）作为权威判据；类 Unix 用
    `os.kill(pid, 0)`（那里的语义确实是探活，`ESRCH` = 不存在）。

    `psutil.pid_exists` 也正确，但 psutil 不在本项目 requirements 里（环境里有
    5.9.0 属于巧合），不为一条测试引入未声明依赖。
    """
    pid = int(pid)
    if os.name == "nt":
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True, timeout=15)
        except Exception:  # noqa: BLE001
            # tasklist 都起不来时保守判「还活着」，让调用方继续等 ——
            # 宁可多等一会儿，也不要因为工具故障谎报「已死」。
            return True
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_code_run_stop_kills_child(tmp_path, monkeypatch):
    async def flow():
        import app.workspace as ws
        monkeypatch.setattr(ws, "WORKSPACES_DIR", tmp_path)
        workspace = tmp_path / "u1"
        workspace.mkdir(parents=True, exist_ok=True)
        pid_file = workspace / "pid.txt"
        code = (
            "import os,time,pathlib\n"
            f"pathlib.Path(r'{pid_file}').write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        task = asyncio.create_task(_run_code_impl(
            "u1", code, "python", 60,
            session_id="s1", generation_id="g1", agent_id="root",
        ))
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.05)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        assert await process_registry.stop_generation("u1", "s1", "g1") >= 1
        out = await asyncio.wait_for(task, timeout=8)
        assert "已停止" in out
        # 轮询到子进程真的消失（判据见 pid_alive 的注释：Windows 不能用 os.kill）。
        # 50 × 0.05s = 2.5s；实测 taskkill /T /F 后 0.15s 内消失，余量充足。
        for _ in range(50):
            if not pid_alive(pid):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail(f"child process still alive after stop (pid={pid})")
    run(flow())


def test_builtin_code_run_passes_agent_scope_to_registry(tmp_path, monkeypatch):
    async def flow():
        seen = {}

        async def fake_run(user_id, code, code_type="python", timeout=0, **kwargs):
            seen.update({"user_id": user_id, "code": code,
                         "code_type": code_type, "timeout": timeout, **kwargs})
            return "ok"

        monkeypatch.setattr("app.builtin_tools._run_code_impl", fake_run)
        tools = {t.name: t for t in __import__("app.builtin_tools", fromlist=["build_builtin_tools"]).build_builtin_tools(
            "u1", session_id="s1", generation_id="g1", team_id="t1", agent_id="sub:a",
        )}
        assert await tools["code_run"].call({"code": "print(1)"}) == "ok"
        assert seen == {
            "user_id": "u1", "code": "print(1)", "code_type": "python",
            "timeout": 60, "root": "", "session_id": "s1",
            "generation_id": "g1", "agent_id": "sub:a",
        }
    run(flow())

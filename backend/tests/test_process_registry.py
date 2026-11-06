import asyncio
import os

import pytest

from app import process_registry
from app.builtin_tools import _run_code_impl


def run(coro):
    return asyncio.run(coro)


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
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("child process still alive after stop")
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

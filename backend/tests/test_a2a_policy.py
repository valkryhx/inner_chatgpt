"""A2A 能力档位（tool policy）、并发配额、审计与终态订阅。

这组测试锁的是「不同意逐次审批」这个产品决定：
- 权限在建 token 时一次性选定，运行期靠 registry 里没有该工具来兜底；
- 默认 full，与网页端完全一致，不改变任何现有行为。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user


def run(coro):
    return asyncio.run(coro)


def enabled_agent(uid):
    """测试用：建 agent 并打开用户级开关（产品默认是关的）。"""
    from app.a2a import store

    run(store.ensure_agent(uid))
    run(store.set_agent_enabled(uid, True))
    return run(store.get_agent(uid))


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    from app.a2a import store

    path = tmp_path / "app.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setenv("INNER_A2A_ENABLED", "true")
    monkeypatch.setenv("INNER_A2A_PUBLIC_BASE_URL", "https://hlaudz.example.test")
    run(db.init_db())
    run(store.init_tables())

    async def seed_user(uid):
        async with aiosqlite.connect(path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users(id,email,username,password_hash,name,role,status,token_ver,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@test.invalid", uid, "x", uid, "user", "active", 0, 0),
            )
            await conn.commit()

    run(seed_user("u1"))
    run(seed_user("u2"))
    yield path


def test_policy_matrix_filters_tools():
    from app.tool_policy import filter_tools, normalize
    from app.tools import Tool

    def mk(n):
        async def call(args):
            return "ok"
        return Tool(n, "", {"type": "object", "properties": {}}, call)

    names = ["file_read", "list_files", "load_skill", "file_write", "file_patch", "code_run"]
    tools = [mk(n) for n in names]

    assert normalize(None) == "full"
    assert normalize("") == "full"
    assert {t.name for t in filter_tools(tools, "full")} == set(names)
    assert {t.name for t in filter_tools(tools, "read_only")} == {"file_read", "list_files", "load_skill"}
    assert filter_tools(tools, "chat_only") == []


def test_policy_rejects_unknown_level():
    from app.tool_policy import normalize

    with pytest.raises(ValueError):
        normalize("root")


def test_token_defaults_to_full_policy(isolated_db):
    from app.a2a import tokens

    _, info = run(tokens.create_token("u1", name="default"))
    assert info["tool_policy"] == "full"


def test_token_stores_policy_and_auth_returns_it(isolated_db):
    from app.a2a import auth, store, tokens

    agent = enabled_agent("u1")
    raw, info = run(tokens.create_token("u1", name="ro", tool_policy="read_only"))
    assert info["tool_policy"] == "read_only"

    ident = run(auth.authenticate(f"Bearer {raw}", agent["agent_id"], None))
    assert ident["tool_policy"] == "read_only"


def test_token_rejects_unknown_policy(isolated_db):
    from app.a2a import tokens

    with pytest.raises(ValueError):
        run(tokens.create_token("u1", name="x", tool_policy="root"))


def _fake_base():
    from app.tools import Tool, ToolRegistry

    def mk(n):
        async def call(args):
            return "ok"
        return Tool(n, "", {"type": "object", "properties": {}}, call)

    class FakeModel:
        model_name = "fake"
        client = object()

    class FakeBaseLoop:
        system_prompt = "SYS"

    class FakeBase:
        model = FakeModel()
        loop = FakeBaseLoop()
        store = object()
        registry = ToolRegistry([mk("spawn_agent"), mk("mcp_thing")])

    return FakeBase()


def _ctx():
    from app.subagent.context import AgentContext

    return AgentContext(user_id="u1", session_id="s1", message_id="m1", generation_id="g1")


def test_turn_runtime_full_policy_keeps_everything(tmp_path, monkeypatch):
    from app import workspace
    from app.subagent import store as sub_store
    from app.subagent.session import build_turn_runtime

    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "s.db")
    run(sub_store.init_tables())

    turn = run(build_turn_runtime(_fake_base(), _ctx()))
    names = set(turn.loop.registry.names())
    for t in ("file_read", "file_write", "code_run", "spawn_agent", "mcp_thing"):
        assert t in names, t


def test_turn_runtime_read_only_drops_write_exec_mcp_and_orchestration(tmp_path, monkeypatch):
    from app import workspace
    from app.subagent import store as sub_store
    from app.subagent.session import build_turn_runtime

    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "s.db")
    run(sub_store.init_tables())

    turn = run(build_turn_runtime(_fake_base(), _ctx(), tool_policy="read_only"))
    names = set(turn.loop.registry.names())
    assert {"file_read", "list_files", "load_skill"} <= names
    for t in ("file_write", "file_patch", "code_run", "spawn_agent", "mcp_thing", "ga_spawn"):
        assert t not in names, t


def test_turn_runtime_chat_only_has_no_tools(tmp_path, monkeypatch):
    from app import workspace
    from app.subagent import store as sub_store
    from app.subagent.session import build_turn_runtime

    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(sub_store, "DB_PATH", tmp_path / "s.db")
    run(sub_store.init_tables())

    turn = run(build_turn_runtime(_fake_base(), _ctx(), tool_policy="chat_only"))
    assert turn.loop.registry.names() == []


def _done_task(agent_id, task_id="task_done", state="TASK_STATE_COMPLETED", text="DONE OK"):
    from app.a2a import store

    run(store.create_task(task_id=task_id, context_id="co_x", user_id="u1",
                          agent_id=agent_id, session_id="s1", message_id="m1",
                          generation_id="g1"))
    run(store.update_task(task_id, state=state,
                          artifacts=[{"artifactId": "answer", "parts": [{"text": text}]}]))


def _app():
    from app.a2a.router import api_router, router

    app = FastAPI()
    app.include_router(router)
    app.include_router(api_router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    return app


def test_subscribe_to_finished_task_returns_terminal_state(isolated_db):
    from app.a2a import store, tokens

    agent = enabled_agent("u1")
    raw, _ = run(tokens.create_token("u1", name="terminal-sub"))
    _done_task(agent["agent_id"])

    body = {"jsonrpc": "2.0", "id": 1, "method": "SubscribeToTask",
            "params": {"id": "task_done"}}
    with TestClient(_app()) as client:
        with client.stream("POST", f"/a2a/{agent['agent_id']}", json=body,
                           headers={"Authorization": f"Bearer {raw}"}) as resp:
            assert resp.status_code == 200
            text = "".join(resp.iter_text())
    assert "DONE OK" in text
    assert "TASK_STATE_COMPLETED" in text
    assert "not streamable" not in text


def test_subscribe_to_restart_failed_task_shows_reason(isolated_db):
    from app.a2a import store, tokens

    agent = enabled_agent("u1")
    raw, _ = run(tokens.create_token("u1", name="restart"))
    _done_task(agent["agent_id"], task_id="task_died", state="TASK_STATE_FAILED", text="")
    run(store.update_task("task_died", error="server restart"))

    body = {"jsonrpc": "2.0", "id": 2, "method": "SubscribeToTask",
            "params": {"id": "task_died"}}
    with TestClient(_app()) as client:
        with client.stream("POST", f"/a2a/{agent['agent_id']}", json=body,
                           headers={"Authorization": f"Bearer {raw}"}) as resp:
            text = "".join(resp.iter_text())
    assert "TASK_STATE_FAILED" in text
    assert "server restart" in text


def test_active_task_quota_is_enforced(isolated_db, monkeypatch):
    from app.a2a import service, store
    from app.a2a.errors import A2AError

    agent = enabled_agent("u1")
    run(store.create_task(task_id="task_busy", context_id="co_busy", user_id="u1",
                          agent_id=agent["agent_id"], session_id="s1", message_id="m0",
                          generation_id="g0", state="TASK_STATE_WORKING"))
    monkeypatch.setenv("INNER_A2A_MAX_ACTIVE_TASKS_PER_USER", "1")

    ident = {"user_id": "u1", "agent_id": agent["agent_id"], "tool_policy": "full"}
    with pytest.raises(A2AError) as exc:
        run(service._prepare_task(ident, {"message": {
            "messageId": "quota-1", "role": "ROLE_USER",
            "parts": [{"text": "hi"}]}}))
    assert exc.value.code == -32010
    assert "active" in exc.value.message.lower()


def test_audit_log_records_jsonrpc_calls(isolated_db):
    from app.a2a import store, tokens

    agent = enabled_agent("u1")
    raw, _ = run(tokens.create_token("u1", name="audit"))
    body = {"jsonrpc": "2.0", "id": 3, "method": "GetTask",
            "params": {"id": "task_missing"}}
    with TestClient(_app()) as client:
        resp = client.post(f"/a2a/{agent['agent_id']}", json=body,
                           headers={"Authorization": f"Bearer {raw}"})
    assert resp.json()["error"]["code"] == -32001

    rows = run(store.list_audit("u1"))
    assert rows, "expected an audit row"
    latest = rows[0]
    assert latest["method"] == "GetTask"
    assert latest["outcome"] == "error"
    assert latest["task_id"] == "task_missing"

"""A2A MVP: per-user agent identity, token auth, JSON-RPC task adapter."""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi import FastAPI, HTTPException
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


def identity_for(agent):
    return {"user_id": agent["user_id"], "agent_id": agent["agent_id"]}


def test_token_is_stored_hashed_and_resolves(isolated_db):
    from app.a2a import auth, store, tokens

    agent = enabled_agent("u1")
    raw, info = run(tokens.create_token("u1", name="cli", scopes=["chat"]))

    assert raw.startswith("hlz_a2a_")
    assert info["agent_id"] == agent["agent_id"]
    row = run(store.find_token_by_hash(tokens.hash_token(raw)))
    assert row is not None
    assert row["token_hash"] != raw
    assert row["prefix"] == raw[:12]

    ident = run(auth.authenticate(f"Bearer {raw}", agent["agent_id"], agent["agent_id"]))
    assert ident["user_id"] == "u1"
    assert ident["agent_id"] == agent["agent_id"]


def test_auth_rejects_missing_wrong_path_and_tenant(isolated_db):
    from app.a2a import auth, store, tokens

    a = enabled_agent("u1")
    b = enabled_agent("u2")
    raw, _ = run(tokens.create_token("u1", name="cli"))

    with pytest.raises(HTTPException) as exc:
        run(auth.authenticate(None, a["agent_id"], None))
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        run(auth.authenticate(f"Bearer {raw}", b["agent_id"], b["agent_id"]))
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        run(auth.authenticate(f"Bearer {raw}", a["agent_id"], "someone-else"))
    assert exc.value.status_code == 403


def test_revoked_token_is_rejected(isolated_db):
    from app.a2a import auth, store, tokens

    agent = enabled_agent("u1")
    raw, info = run(tokens.create_token("u1", name="cli"))
    run(store.revoke_token("u1", info["id"]))
    with pytest.raises(HTTPException) as exc:
        run(auth.authenticate(f"Bearer {raw}", agent["agent_id"], None))
    assert exc.value.status_code == 401


def test_agent_card_is_per_user_and_v1(isolated_db):
    from app.a2a import cards, store

    agent = enabled_agent("u1")
    card = cards.build_agent_card(agent["agent_id"], "https://hlaudz.example.test")
    iface = card["supportedInterfaces"][0]
    assert iface["url"] == f"https://hlaudz.example.test/a2a/{agent['agent_id']}"
    assert iface["protocolBinding"] == "JSONRPC"
    assert iface["protocolVersion"] == "1.0"
    assert iface["tenant"] == agent["agent_id"]
    assert card["capabilities"]["streaming"] is True
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"


def _fake_stream(text="hello world"):
    from app.sse import sse_frame

    async def stream(user_id, session_id, text_in, generation_id, message_id,
                     model_id="", stop_event=None, tool_policy="full"):
        yield sse_frame("message_start", {"message_id": message_id})
        yield sse_frame("text_delta", {"text": text})
        yield sse_frame("done", {"message_id": message_id, "status": "complete", "model": "fake"})

    return stream


def test_send_message_completes_and_persists(isolated_db, monkeypatch):
    from app.a2a import service, store
    from app.routers import chat

    agent = enabled_agent("u1")
    monkeypatch.setattr(chat, "_event_stream", _fake_stream("hello world"))

    task = run(service.send_message(
        identity_for(agent),
        {"message": {"messageId": "m1", "role": "ROLE_USER",
                     "parts": [{"text": "hi"}]}},
        sync_timeout=2.0,
    ))
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0]["text"] == "hello world"
    row = run(store.get_task("u1", agent["agent_id"], task["id"]))
    assert row is not None and row["state"] == "TASK_STATE_COMPLETED"


def test_send_message_is_idempotent(isolated_db, monkeypatch):
    from app.a2a import service, store
    from app.routers import chat

    agent = enabled_agent("u1")
    calls = 0

    async def stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        async for frame in _fake_stream("once")(*args, **kwargs):
            yield frame

    monkeypatch.setattr(chat, "_event_stream", stream)
    params = {"message": {"messageId": "same", "role": "ROLE_USER",
                          "parts": [{"text": "hi"}]}}
    first = run(service.send_message(identity_for(agent), params, sync_timeout=2.0))
    second = run(service.send_message(identity_for(agent), params, sync_timeout=2.0))
    assert first["id"] == second["id"]
    assert calls == 1


def _stream_fake(texts=("hello ", "world")):
    from app.sse import sse_frame

    async def stream(user_id, session_id, text_in, generation_id, message_id,
                     model_id="", stop_event=None, tool_policy="full"):
        yield sse_frame("message_start", {"message_id": message_id})
        for text in texts:
            yield sse_frame("text_delta", {"text": text})
        yield sse_frame("done", {"message_id": message_id, "status": "complete"})

    return stream


def test_stream_send_emits_task_artifact_and_terminal(isolated_db, monkeypatch):
    import json
    from app.a2a import service, store
    from app.routers import chat

    agent = enabled_agent("u1")
    monkeypatch.setattr(chat, "_event_stream", _stream_fake())

    async def scenario():
        row = await service.prepare_stream(
            identity_for(agent),
            {"message": {"messageId": "stream-1", "role": "ROLE_USER",
                         "parts": [{"text": "hi"}]}},
        )
        chunks = []
        async for chunk in service.stream_row(row, 7):
            chunks.append(chunk)
        return chunks, await store.get_task("u1", agent["agent_id"], row["task_id"])

    chunks, row = run(scenario())
    payloads = [json.loads(c.split("data: ", 1)[1])["result"] for c in chunks]
    assert "task" in payloads[0]
    artifacts = [p["artifactUpdate"] for p in payloads if "artifactUpdate" in p]
    text = "".join(a["artifact"]["parts"][0]["text"] for a in artifacts
                   if a["artifact"].get("parts"))
    assert text == "hello world"
    assert payloads[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert row["state"] == "TASK_STATE_COMPLETED"


def test_stream_disconnect_does_not_cancel_task(isolated_db, monkeypatch):
    from app.a2a import service, store
    from app.routers import chat

    agent = enabled_agent("u1")

    async def waiting(*args, **kwargs):
        from app.sse import sse_frame
        yield sse_frame("message_start", {"message_id": args[4]})
        await asyncio.Event().wait()

    monkeypatch.setattr(chat, "_event_stream", waiting)

    async def scenario():
        row = await service.prepare_stream(
            identity_for(agent),
            {"message": {"messageId": "stream-2", "role": "ROLE_USER",
                         "parts": [{"text": "hi"}]}},
        )
        agen = service.stream_row(row, 8)
        await agen.__anext__()
        await agen.aclose()
        await asyncio.sleep(0.02)
        before = (await store.get_task("u1", agent["agent_id"], row["task_id"]))["state"]
        await service.cancel_task(identity_for(agent), row["task_id"])
        after = (await store.get_task("u1", agent["agent_id"], row["task_id"]))["state"]
        return before, after

    before, after = run(scenario())
    assert before not in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                          "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
    assert after == "TASK_STATE_CANCELED"


def test_get_task_is_scoped_to_agent(isolated_db, monkeypatch):
    from app.a2a import service, store
    from app.routers import chat

    a = enabled_agent("u1")
    b = enabled_agent("u2")
    monkeypatch.setattr(chat, "_event_stream", _fake_stream("ok"))
    task = run(service.send_message(
        identity_for(a), {"message": {"messageId": "m1", "role": "ROLE_USER",
                                     "parts": [{"text": "hi"}]}}, sync_timeout=2.0,
    ))
    assert run(service.get_task(identity_for(b), task["id"])) is None


def test_cancel_task_calls_existing_stop_path(isolated_db, monkeypatch):
    from app.a2a import service, store
    from app.routers import chat
    from app.schemas import StopResult

    agent = enabled_agent("u1")
    seen: dict = {}

    async def stream(user_id, session_id, text, generation_id, message_id,
                     model_id="", stop_event=None, tool_policy="full"):
        from app.sse import sse_frame
        yield sse_frame("message_start", {"message_id": message_id})
        await asyncio.Event().wait()

    async def fake_stop(session_id, body, user):
        seen["session_id"] = session_id
        seen["generation_id"] = body.generation_id
        seen["user"] = user
        return StopResult(ok=True)

    monkeypatch.setattr(chat, "_event_stream", stream)
    monkeypatch.setattr(chat, "stop_generation", fake_stop)

    async def scenario():
        task = await service.send_message(
            identity_for(agent), {"message": {"messageId": "m1", "role": "ROLE_USER",
                                              "parts": [{"text": "hi"}]}},
            return_immediately=True,
        )
        await asyncio.sleep(0.01)
        await service.cancel_task(identity_for(agent), task["id"])
        return await store.get_task("u1", agent["agent_id"], task["id"])

    row = run(scenario())
    assert seen["generation_id"]
    assert seen["user"]["id"] == "u1"
    assert row["state"] == "TASK_STATE_CANCELED"


def test_router_card_and_jsonrpc_error_envelope(isolated_db, monkeypatch):
    from app.a2a.router import api_router, router

    app = FastAPI()
    app.include_router(router)
    app.include_router(api_router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}

    with TestClient(app) as client:
        assert client.post("/api/a2a/tokens",
                          json={"name": "bad", "scopes": ["tools:run"]}).status_code == 400
        created = client.post("/api/a2a/tokens", json={"name": "cli"}).json()
        raw = created["token"]
        agent_id = created["agent_id"]
        from app.a2a import store as a2a_store
        run(a2a_store.set_agent_enabled("u1", True))
        card = client.get(f"/a2a/{agent_id}/.well-known/agent-card.json")
        assert card.status_code == 200
        assert card.json()["supportedInterfaces"][0]["tenant"] == agent_id
        assert client.get(f"/a2a/{agent_id}").json()["name"] == "Hlaudz Agent"
        assert created["url"].startswith("https://hlaudz.example.test/a2a/")
        assert created["card_url"].startswith("https://hlaudz.example.test/a2a/")

        bad = client.post(f"/a2a/{agent_id}", json={"jsonrpc": "2.0", "id": 1,
                                                    "method": "NoSuchMethod",
                                                    "params": {}},
                          headers={"Authorization": f"Bearer {raw}"})
        assert bad.status_code == 200
        assert bad.json()["error"]["code"] == -32601


def test_router_streaming_sse(isolated_db, monkeypatch):
    from app.a2a.router import api_router, router
    from app.routers import chat

    app = FastAPI()
    app.include_router(router)
    app.include_router(api_router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    monkeypatch.setattr(chat, "_event_stream", _stream_fake(("SSE ", "OK")))

    with TestClient(app) as client:
        created = client.post("/api/a2a/tokens", json={"name": "sse"}).json()
        raw, agent_id = created["token"], created["agent_id"]
        from app.a2a import store as a2a_store
        run(a2a_store.set_agent_enabled("u1", True))
        body = {
            "jsonrpc": "2.0", "id": 10, "method": "SendStreamingMessage",
            "params": {"message": {"messageId": "sse-1", "role": "ROLE_USER",
                                   "parts": [{"text": "hi"}]}},
        }
        with client.stream("POST", f"/a2a/{agent_id}", json=body,
                           headers={"Authorization": f"Bearer {raw}"}) as resp:
            assert resp.status_code == 200
            text = "".join(resp.iter_text())
    assert "data: " in text
    assert "artifactUpdate" in text
    assert "TASK_STATE_COMPLETED" in text
    assert "SSE " in text and "OK" in text


def test_disabled_endpoint_is_404(isolated_db, monkeypatch):
    from app.a2a import store
    from app.a2a.router import router

    agent = enabled_agent("u1")
    monkeypatch.setenv("INNER_A2A_ENABLED", "false")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get(
            f"/a2a/{agent['agent_id']}/.well-known/agent-card.json"
        ).status_code == 404

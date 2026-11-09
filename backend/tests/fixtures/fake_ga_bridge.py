"""Minimal ACP agent. Env:
FAKE_GA_MODE = chunks|tools|permission|hang|crash|malformed
FAKE_GA_STDERR_LINES = int (flood stderr *before* reading stdin)
FAKE_GA_LOG = path (append every received message as one JSON line)
"""
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_GA_MODE", "chunks")
LOG = os.environ.get("FAKE_GA_LOG")


def log(obj):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid, "update": upd}})


def main():
    for _ in range(int(os.environ.get("FAKE_GA_STDERR_LINES", "0") or 0)):
        sys.stderr.write("[FAKE] noise line to fill the pipe\n")
    sys.stderr.flush()
    pending = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        log(msg)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        sid = params.get("sessionId")
        if MODE == "crash" and method == "session/prompt":
            os._exit(3)
        if MODE == "malformed":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1, "agentCapabilities": {},
                "agentInfo": {"name": "fake-ga", "version": "0.0.0"}, "authMethods": []}}
            if MODE == "split":
                payload = json.dumps(resp, ensure_ascii=False)
                sys.stdout.write(payload[:12])
                sys.stdout.flush()
                time.sleep(0.05)
                sys.stdout.write(payload[12:] + "\n")
                sys.stdout.flush()
            else:
                send(resp)
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "sessionId": "ga_fake_1",
                "modes": {"currentModeId": "full_access", "availableModes": []},
                "configOptions": None}})
        elif method == "session/set_mode":
            send({"jsonrpc": "2.0", "id": rid, "result": {"modeId": params.get("modeId")}})
        elif method == "session/prompt":
            if MODE == "hang":
                pending[sid] = rid
            elif MODE == "permission":
                send({"jsonrpc": "2.0", "id": "perm_fake",
                      "method": "session/request_permission",
                      "params": {"sessionId": sid,
                                 "toolCall": {"toolCallId": "t1", "title": "file_write",
                                              "kind": "edit", "status": "pending"},
                                 "options": [{"optionId": "allow_once", "kind": "allow_once"},
                                             {"optionId": "reject_once", "kind": "reject_once"}]}})
                pending[sid] = rid
            else:
                if MODE == "tools":
                    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1",
                                 "title": "file_read", "kind": "read", "status": "in_progress",
                                 "rawInput": {"preview": "a.py"}})
                    update(sid, {"sessionUpdate": "agent_message_chunk",
                                 "content": {"type": "text", "text": "reading..."}})
                    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                                 "status": "completed",
                                 "content": [{"type": "content",
                                              "content": {"type": "text", "text": "ok"}}]})
                    update(sid, {"sessionUpdate": "agent_message_chunk",
                                 "content": {"type": "text", "text": "FINAL"}})
                else:
                    for chunk in ("hello ", "world"):
                        update(sid, {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": chunk}})
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            for s, prid in list(pending.items()):
                send({"jsonrpc": "2.0", "id": prid, "result": {"stopReason": "cancelled"}})
                pending.pop(s)
        elif method == "session/close":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None and method is None:
            for s, prid in list(pending.items()):
                update(s, {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "answered"}})
                send({"jsonrpc": "2.0", "id": prid, "result": {"stopReason": "end_turn"}})
                pending.pop(s)


if __name__ == "__main__":
    main()

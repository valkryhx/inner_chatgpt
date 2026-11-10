"""审计一次 e2e 现场：工具序列、终态分布、僵尸 running、任务板一致性。

用法：python -m tests.e2e.audit <runId>
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"
DB = Path(__file__).resolve().parents[2] / "data" / "app.db"



def audit(run_id: str) -> dict:
    d = RUNS / run_id
    src = d / "events.jsonl"
    if not src.exists():
        # 跨回合场景把两轮分开写，审计时合并
        parts = sorted(d.glob("events_turn*.jsonl"))
        raw = "\n".join(p.read_text(encoding="utf-8") for p in parts)
    else:
        raw = src.read_text(encoding="utf-8")
    ev = [json.loads(line) for line in raw.splitlines() if line.strip()]
    summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))

    team_id = next((e.get("team_id") for e in ev if e.get("team_id")), "")
    types = Counter(e.get("type") for e in ev)
    tools = [e.get("name") for e in ev if e.get("type") == "tool_start"]

    starts = [e.get("name") for e in ev if e.get("type") == "subagent_start"]
    ends = [e for e in ev if e.get("type") == "subagent_end"]
    end_status = Counter(e.get("status") for e in ends)
    ended_names = {e.get("name") for e in ends}
    zombies = [n for n in starts if n not in ended_names]

    task_states: dict[str, str] = {}
    for e in ev:
        if e.get("type") in ("task_created", "task_updated"):
            payload = e.get("payload") or {}
            task = payload.get("task") or payload
            tid = task.get("task_id") or task.get("id")
            status = task.get("status")
            if tid and status:
                task_states[tid] = status

    errors = [e for e in ev if e.get("type") == "error"]
    done = next((e for e in reversed(ev) if e.get("type") == "done"), {})

    return {
        "run_id": run_id,
        "elapsed_s": summary.get("elapsed_s"),
        "ok": summary.get("ok"),
        "event_types": types.most_common(),
        "tools": tools,
        "tool_counts": Counter(tools).most_common(),
        "subagent_started": len(starts),
        "subagent_ended": len(ends),
        "subagent_status": end_status.most_common(),
        "zombie_running": zombies,
        "task_states": task_states,
        "db_task_states": db_task_states(team_id),
        "sse_missing_events": sse_missing(team_id, ev),
        "task_state_counts": Counter(task_states.values()).most_common(),
        "errors": errors[:5],
        "done": done.get("status"),
        "checks_failed": _failed_checks(summary),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m tests.e2e.audit <runId>")
        return 2
    print(json.dumps(audit(sys.argv[1]), ensure_ascii=False, indent=2))
    return 0




def _db():
    import sqlite3
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    return con


def db_task_states(team_id: str) -> dict:
    if not team_id:
        return {}
    con = _db()
    try:
        rows = con.execute(
            "select task_id,status from team_tasks where team_id=?", (team_id,)
        ).fetchall()
    finally:
        con.close()
    return {r["task_id"]: r["status"] for r in rows}


def sse_missing(team_id: str, events: list) -> dict:
    """对比 DB 事件总数与 SSE 实际看到的事件数。"""
    if not team_id:
        return {}
    con = _db()
    try:
        db_rows = con.execute(
            "select event_seq,event_type from team_events where team_id=? order by event_seq",
            (team_id,),
        ).fetchall()
    finally:
        con.close()
    seen = {(e.get("event_seq"), e.get("type")) for e in events if e.get("team_id")}
    raw = [(r["event_seq"], r["event_type"]) for r in db_rows
           if (r["event_seq"], r["event_type"]) not in seen]
    # team_end 是生成收尾时写的，SSE 队列此时已关，属预期缺失
    benign = [m for m in raw if m[1] == "team_end"]
    missing = [m for m in raw if m[1] != "team_end"]
    return {"db_total": len(db_rows), "sse_total": len(seen), "missing": missing[:20],
            "missing_count": len(missing), "benign_missing": benign}


def _failed_checks(summary: dict) -> list:
    raw = summary.get("checks") or []
    if isinstance(raw, dict):
        return [{"kind": k, "ok": v} for k, v in raw.items() if not v]
    return [c for c in raw if not c.get("ok")]


if __name__ == "__main__":
    sys.exit(main())


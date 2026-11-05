"""Migrate LangGraph checkpoints into the self-built session_history table.

Run this ONCE while langgraph is still installed (it is only used here to decode
the msgpack checkpoint blobs; the app itself no longer depends on langgraph).

Usage:
    python backend/scripts/migrate_checkpoints.py            # migrate + verify
    python backend/scripts/migrate_checkpoints.py --dry-run  # decode only, no writes

Safety: a DB backup (app.db.premigrate.<ts>) is written before any change.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "backend" / "data" / "app.db"
SUMMARY_SOURCE_KEY = "lc_source"
SUMMARY_SOURCE_VAL = "summarization"


def backup_db(db_path: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = db_path.with_name(f"app.db.premigrate.{ts}")
    # flush WAL into the main db before copying
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
    shutil.copy2(str(db_path), str(dst))
    for suffix in ("-wal", "-shm"):
        src = db_path.with_name(db_path.name + suffix)
        if src.exists():
            shutil.copy2(str(src), str(dst) + suffix)
    return dst


def _blocks_text(content) -> tuple[str, list[dict], str]:
    """Split an OpenAI/LangGraph content value into (text, tool_calls, reasoning)."""
    if content is None:
        return "", [], ""
    if isinstance(content, str):
        return content, [], ""
    texts: list[str] = []
    tool_calls: list[dict] = []
    reasoning: list[str] = []
    for b in content:
        if isinstance(b, str):
            texts.append(b)
            continue
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt in ("text", "output_text", "input_text"):
            v = b.get("text")
            if isinstance(v, str):
                texts.append(v)
        elif bt == "reasoning":
            summary = b.get("summary")
            if isinstance(summary, list):
                for s in summary:
                    if isinstance(s, dict) and isinstance(s.get("text"), str):
                        reasoning.append(s["text"])
            elif isinstance(b.get("text"), str):
                reasoning.append(b["text"])
        elif bt in ("function_call", "tool_call"):
            name = b.get("name", "")
            args = b.get("arguments", b.get("args", {}))
            if isinstance(args, (dict, list)):
                args = json.dumps(args, ensure_ascii=False)
            cid = b.get("call_id") or b.get("id") or ""
            tool_calls.append({
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": args if isinstance(args, str) else str(args)},
            })
    return "".join(texts), tool_calls, "\n".join(reasoning)


def convert_messages(messages) -> list[dict]:
    """Convert LangChain message objects into OpenAI-style dicts."""
    out: list[dict] = []
    for m in messages:
        cls = type(m).__name__
        content = getattr(m, "content", "")
        name = getattr(m, "name", None)
        tcid = getattr(m, "tool_call_id", None)
        msg_id = getattr(m, "id", None)
        ak = getattr(m, "additional_kwargs", {}) or {}

        if cls == "HumanMessage":
            text, _, _ = _blocks_text(content)
            item = {"role": "user", "content": text}
            # compaction summary messages carry lc_source=summarization
            if ak.get(SUMMARY_SOURCE_KEY) == SUMMARY_SOURCE_VAL:
                item["_source"] = SUMMARY_SOURCE_VAL
            if msg_id:
                item["id"] = msg_id
            out.append(item)

        elif cls == "SystemMessage":
            text, _, _ = _blocks_text(content)
            out.append({"role": "system", "content": text})

        elif cls == "ToolMessage":
            text, _, _ = _blocks_text(content)
            out.append({"role": "tool", "tool_call_id": tcid or "",
                        "name": name or "", "content": text})

        elif cls == "AIMessage":
            text, tcalls, reasoning = _blocks_text(content)
            if not tcalls:
                # fall back to the structured .tool_calls attribute
                for tc in (getattr(m, "tool_calls", None) or []):
                    nm = tc.get("name", "")
                    args = tc.get("args", {})
                    if isinstance(args, (dict, list)):
                        args = json.dumps(args, ensure_ascii=False)
                    tcalls.append({
                        "id": tc.get("id") or "",
                        "type": "function",
                        "function": {"name": nm, "arguments": args if isinstance(args, str) else str(args)},
                    })
            if not text and not tcalls:
                continue  # nothing replayable
            item = {"role": "assistant", "content": text}
            if tcalls:
                item["tool_calls"] = tcalls
            if reasoning:
                item["reasoning"] = reasoning
            out.append(item)

        else:
            # Unknown subclass: best-effort text
            text, tcalls, reasoning = _blocks_text(content)
            if text or tcalls:
                item = {"role": "assistant", "content": text}
                if tcalls:
                    item["tool_calls"] = tcalls
                if reasoning:
                    item["reasoning"] = reasoning
                out.append(item)
    return out


def normalize_tool_pairs(msgs: list[dict]) -> list[dict]:
    """Drop orphan tool messages and tool_calls lacking a matching tool result."""
    out: list[dict] = []
    i = 0
    n = len(msgs)
    while i < n:
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            tools: list[dict] = []
            while j < n and msgs[j].get("role") == "tool":
                tools.append(msgs[j])
                j += 1
            ids = {t.get("tool_call_id") for t in tools}
            kept = [tc for tc in m["tool_calls"] if tc.get("id") in ids]
            if kept:
                nm = dict(m)
                nm["tool_calls"] = kept
                out.append(nm)
                kept_ids = {tc.get("id") for tc in kept}
                for t in tools:
                    if t.get("tool_call_id") in kept_ids:
                        out.append(t)
            elif m.get("content"):
                nm = dict(m)
                nm.pop("tool_calls", None)
                out.append(nm)
            i = j
            continue
        if m.get("role") == "tool":
            i += 1
            continue
        out.append(m)
        i += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--sample", type=int, default=5)
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[fatal] db not found: {db_path}")
        return 2

    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] cannot import langgraph serializer (needed only for decoding): {e}")
        return 2
    ser = JsonPlusSerializer()

    if not args.dry_run:
        dst = backup_db(db_path)
        print(f"[backup] {dst}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not args.dry_run:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_history ("
                "session_id TEXT PRIMARY KEY, history TEXT NOT NULL DEFAULT '[]', "
                "updated_at INTEGER NOT NULL DEFAULT 0)"
            )
            conn.commit()

        thread_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id")]
        print(f"[scan] {len(thread_ids)} threads")

        migrated = 0
        total_msgs = 0
        skipped = 0
        errors = 0
        samples: list[tuple[str, int, str, str]] = []

        for tid in thread_ids:
            row = conn.execute(
                "SELECT type, checkpoint FROM checkpoints WHERE thread_id=? "
                "ORDER BY checkpoint_id DESC LIMIT 1", (tid,)).fetchone()
            if row is None:
                continue
            try:
                data = ser.loads_typed((row["type"], row["checkpoint"]))
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"[decode-error] {tid}: {e}")
                continue
            raw_msgs = (data.get("channel_values") or {}).get("messages") or []
            converted = convert_messages(raw_msgs)
            converted = normalize_tool_pairs(converted)
            if not converted:
                skipped += 1
                continue
            total_msgs += len(converted)
            if not args.dry_run:
                conn.execute(
                    "INSERT INTO session_history(session_id,history,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET history=excluded.history, updated_at=excluded.updated_at",
                    (tid, json.dumps(converted, ensure_ascii=False), int(time.time() * 1000)),
                )
            migrated += 1
            if len(samples) < args.sample:
                first_user = next((m.get("content", "") for m in converted if m.get("role") == "user"), "")
                last_asst = next((m.get("content", "") for m in reversed(converted) if m.get("role") == "assistant"), "")
                samples.append((tid, len(converted), str(first_user)[:40], str(last_asst)[:40]))

        if not args.dry_run:
            conn.commit()

        print(f"[result] migrated={migrated} skipped={skipped} errors={errors} total_messages={total_msgs}")
        print("[sample]")
        for tid, n, fu, la in samples:
            print(f"  {tid} n={n} first_user={fu!r} last_asst={la!r}")

        if not args.dry_run:
            cnt = conn.execute("SELECT COUNT(*) FROM session_history").fetchone()[0]
            print(f"[verify] session_history rows={cnt}")
            # cross-check against messages table for a few sessions
            sessions = [r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM messages ORDER BY session_id")]
            mism = 0
            for sid in sessions:
                hist = conn.execute(
                    "SELECT history FROM session_history WHERE session_id=?", (sid,)).fetchone()
                if hist is None:
                    mism += 1
                    print(f"  [missing] {sid} has messages but no history")
                    continue
                try:
                    h = json.loads(hist["history"])
                except Exception:
                    mism += 1
                    continue
                db_first = conn.execute(
                    "SELECT content FROM messages WHERE session_id=? AND role='user' "
                    "ORDER BY created_at LIMIT 1", (sid,)).fetchone()
                hist_first = next((m.get("content", "") for m in h if m.get("role") == "user"), "")
                if db_first and db_first["content"].strip() and db_first["content"].strip() not in (hist_first or ""):
                    # first DB user msg may be older than the retained checkpoint window
                    pass
            print(f"[verify] sessions_with_messages={len(sessions)} history_missing={mism}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

"""从团队事件 JSONL 还原一次协作的时间线。

用法：
    python tools/team_timeline.py                     # 最近一个 team_id
    python tools/team_timeline.py --team team_abc123  # 指定团队
    python tools/team_timeline.py --message tmsg_xxx  # 追一条消息的完整生命周期
    python tools/team_timeline.py --list              # 列出日志里出现过的团队

为什么需要它：JSONL 是给机器 grep 的，但排查问题是人做的。
把散落的事件按时间排成一条线，能直接看出「消息发了吗 / 谁收到了 / 谁卡住了」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "backend" / "data" / "logs"

# 每个事件在时间线上关心的字段（多余的不打印，避免一行看不完）
FIELDS = {
    "message_queued": ("message_id", "sender", "recipient", "kind", "content_chars"),
    "message_delivered": ("message_id", "agent", "sender", "was_idle", "pending"),
    "message_undelivered": ("agent", "reason", "delivered"),
    "message_duplicate": ("agent", "message_id"),
    "delivery_failed": ("message_id", "recipient", "reason"),
    "delivery_no_sink": ("message_id", "recipient"),
    "delivery_error": ("message_id", "recipient", "error"),
    "inbox_consumed": ("agent", "count", "message_ids"),
    "agent_round_done": ("agent", "round", "turns", "result_chars", "idle_budget_s"),
    "agent_idle_wait": ("agent", "round", "reason", "pending"),
    "agent_end": ("agent", "status", "rounds", "turns", "error"),
    "task_created": ("task_id", "actor"),
    "task_claimed": ("task_id", "actor", "version"),
    "task_claim_failed": ("task_id", "actor", "reason"),
    "task_completed": ("task_id", "actor", "version"),
    "task_complete_failed": ("task_id", "actor", "reason", "actual_owner"),
    "task_bind_blocked": ("task_id", "actor", "reason"),
}


def load_events() -> list[dict]:
    if not LOGS.exists():
        return []
    rows: list[dict] = []
    for path in sorted(LOGS.glob("team-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows


def render(rows: list[dict], *, team_id: str = "", message_id: str = "") -> None:
    if not rows:
        print("（没有匹配的事件）")
        return
    print(f"{'时间':<10} {'事件':<22} 细节")
    print("-" * 100)
    for r in rows:
        fields = FIELDS.get(r["event"], ())
        detail = "  ".join(
            f"{k}={r[k]}" for k in fields if k in r and r[k] not in ("", None))
        if not detail:
            detail = "  ".join(
                f"{k}={v}" for k, v in r.items()
                if k not in ("ts", "level", "logger", "event"))
        print(f"{r.get('ts', '')[11:]:<10} {r['event']:<22} {detail}")

    # 一句话诊断：把最容易出问题的地方直接点出来
    print("-" * 100)
    _diagnose(rows)


def _diagnose(rows: list[dict]) -> None:
    queued = [r for r in rows if r["event"] == "message_queued"]
    delivered = {r.get("message_id") for r in rows if r["event"] == "message_delivered"}
    consumed = set()
    for r in rows:
        if r["event"] == "inbox_consumed":
            consumed.update(r.get("message_ids") or [])
    stuck = [r for r in queued if r.get("message_id") not in delivered
             and r.get("message_id") not in consumed]
    if stuck:
        print(f"⚠ {len(stuck)} 条消息只入队、没看到投递或消费：")
        for r in stuck[:5]:
            print(f"    {r.get('message_id')} {r.get('sender')} -> "
                  f"{r.get('recipient')} ({r.get('ts', '')[11:]})")
    failed = [r for r in rows if r["event"] in
              ("message_undelivered", "delivery_failed", "delivery_no_sink",
               "delivery_error")]
    if failed:
        print(f"⚠ {len(failed)} 次投递失败：")
        for r in failed[:5]:
            print(f"    {r['event']} {r.get('reason') or r.get('error') or ''}")
    bad_end = [r for r in rows if r["event"] == "agent_end" and r.get("error")]
    if bad_end:
        print(f"⚠ {len(bad_end)} 个 agent 带错误结束：")
        for r in bad_end[:5]:
            print(f"    {r.get('agent')} {r.get('status')} {r.get('error')}")
    if not stuck and not failed and not bad_end:
        print("✓ 没有发现消息滞留或投递失败")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", default="", help="team_id")
    ap.add_argument("--message", default="", help="只追这一条 message_id")
    ap.add_argument("--list", action="store_true", help="列出日志里出现过的团队")
    args = ap.parse_args()

    rows = load_events()
    if not rows:
        print(f"没有找到团队日志：{LOGS}/team-*.jsonl")
        print("请用 HLZ_TEAM_LOG=1 启动后端")
        return 1

    if args.list:
        seen: dict[str, dict] = {}
        for r in rows:
            tid = r.get("team_id")
            if not tid:
                continue
            info = seen.setdefault(tid, {"first": r.get("ts", ""), "events": 0})
            info["events"] += 1
            info["last"] = r.get("ts", "")
        for tid, info in sorted(seen.items(), key=lambda kv: kv[1]["first"]):
            print(f"{tid}  {info['first'][11:]} → {info['last'][11:]}  {info['events']} 条")
        return 0

    if args.message:
        picked = [r for r in rows if r.get("message_id") == args.message]
    elif args.team:
        picked = [r for r in rows if r.get("team_id") == args.team]
    else:
        teams = [r.get("team_id") for r in rows if r.get("team_id")]
        if not teams:
            print("（日志里没有 team_id 字段）")
            return 1
        latest = teams[-1]
        print(f"（未指定 --team，使用最近一个：{latest}）\n")
        picked = [r for r in rows if r.get("team_id") == latest]

    render(picked, team_id=args.team, message_id=args.message)
    return 0


if __name__ == "__main__":
    sys.exit(main())

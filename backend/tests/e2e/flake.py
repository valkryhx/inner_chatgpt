"""稳定性复跑：同一批场景连跑 N 轮，把「真 flake」和「上游 502/超时」分开。

用法（在 backend/ 下）：
    python -m tests.e2e.flake --case S1,S2,S3 --rounds 3

判定：
    502 / ConnectError / 网关错误 → infra，不算 flake（单独计数）
    同一场景在不同轮次结论不一致 → flake
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from . import harness
from .run import cases as all_cases

HERE = Path(__file__).resolve().parent
INFRA_MARKERS = ("502 Bad Gateway", "ConnectError", "Connection", "timeout", "503", "504")


def _infra(run_dir: Path) -> str:
    """只看 error 事件本身，避免把模型正文里的「timeout」误判成基础设施故障。"""
    p = run_dir / "events.jsonl"
    if not p.exists():
        return ""
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") not in ("error", "_transport_error"):
                continue
            msg = str(ev.get("error") or "")
            for m in INFRA_MARKERS:
                if m in msg:
                    return m
            return "error"
    return ""


async def run_round(names: list[str], table: dict, round_no: int) -> list[dict]:
    rows = []
    for name in names:
        case = dict(table[name])
        case["name"] = f"flake-{name}-r{round_no}"
        started = time.time()
        try:
            result = await harness.run_case(case)
            infra = _infra(Path(result["dir"]))
            rows.append({"case": name, "round": round_no, "ok": result["ok"],
                         "infra": infra, "run_id": result["run_id"],
                         "elapsed_s": round(time.time() - started, 1),
                         "failed": [c["kind"] + ":" + str(c["name"])
                                    for c in result["checks"] if not c["ok"]]})
        except Exception as exc:  # noqa: BLE001
            rows.append({"case": name, "round": round_no, "ok": False,
                         "infra": f"{type(exc).__name__}", "run_id": "-",
                         "elapsed_s": round(time.time() - started, 1), "failed": []})
        mark = "PASS" if rows[-1]["ok"] else ("INFRA" if rows[-1]["infra"] else "FAIL")
        print(f"[{mark}] {name}#{round_no} {rows[-1]['elapsed_s']}s "
              f"run={rows[-1]['run_id']} {rows[-1]['infra']}", flush=True)
    return rows


async def main_async(args: argparse.Namespace) -> int:
    table = all_cases()
    names = [n.strip() for n in args.case.split(",") if n.strip()]
    bad = [n for n in names if n not in table]
    if bad:
        print("未知场景:", bad)
        return 2
    rows: list[dict] = []
    for r in range(1, args.rounds + 1):
        print(f"== 第 {r}/{args.rounds} 轮 ==", flush=True)
        rows += await run_round(names, table, r)
    out = HERE / "runs" / f"flake-{int(time.time())}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    verdict = {}
    for n in names:
        rs = [r for r in rows if r["case"] == n and not r["infra"]]
        if len(rs) < 2:
            verdict[n] = "样本不足"
        elif all(r["ok"] for r in rs):
            verdict[n] = "稳定通过"
        elif not any(r["ok"] for r in rs):
            verdict[n] = "稳定失败（真缺陷）"
        else:
            verdict[n] = "FLAKE"
    lines = ["# 稳定性复跑", "", "| 场景 | 结论 | 有效次数 | 基础设施失败 |", "| --- | --- | --- | --- |"]
    for n in names:
        inf = sum(1 for r in rows if r["case"] == n and r["infra"])
        lines.append(f"| {n} | {verdict[n]} | {sum(1 for r in rows if r['case']==n) - inf} | {inf} |")
    p = out / "report.md"
    p.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    print("report:", p, flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="S1,S2,S3,S4,S5,S6,S7,S8,S11")
    ap.add_argument("--rounds", type=int, default=3)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

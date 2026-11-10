"""提示词消融实验：同一批任务，分别用改造前 / 改造后的基础提示词跑。

用法（在 backend/ 下）：
    python -m tests.e2e.ablation                 # 两变体 × 全部用例
    python -m tests.e2e.ablation --variant new   # 只跑一个变体
    python -m tests.e2e.ablation --rounds 2      # 每用例跑 2 次

指标全部从 SSE 事件与落盘产物自动算出，不做人工判读：
    tool_ok      工具选择是否符合预期（期望出现 / 明确不该出现）
    skill_load   无谓的 load_skill 次数（本批任务都不需要加载 skill）
    rounds       工具轮次（≈ 回合数，越少越直接）
    evidence     声称完成时是否留下可核对的痕迹（读回 / 运行输出）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import harness

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
PORTS = {"new": 8011, "legacy": 8012}

CASES: list[dict] = [
    {
        "key": "glob_only",
        "prompt": "列出你工作区里所有 .py 文件的路径（只列名字，不要打开读内容）。一个都没有就直接说没有。",
        "expect_tools": ["file_glob"],
        "forbid_tools": ["code_run"],
        "timeout_s": 240,
    },
    {
        "key": "grep_only",
        "prompt": "在你工作区里搜索包含 TODO 的位置。没有匹配就直接说没有，不要新建文件。",
        "expect_tools": ["file_grep"],
        "forbid_tools": ["code_run"],
        "timeout_s": 240,
    },
    {
        "key": "calc_code_run",
        "prompt": "用代码实际计算 1 到 100 每个数的平方和，不要心算，把结果告诉我。",
        "expect_tools": ["code_run"],
        "sentinel": "338350",
        "timeout_s": 300,
    },
    {
        "key": "fib_20",
        "prompt": "写一个脚本算斐波那契数列第 20 项（第一项和第二项都算 1），实际运行，把结果告诉我。",
        "expect_tools": ["code_run"],
        "sentinel": "6765",
        "timeout_s": 300,
    },
    {
        "key": "write_then_read",
        "prompt": "在你工作区里建一个 a.txt，内容写 hello，然后读回来确认内容真的是 hello，再告诉我。",
        "expect_tools": ["file_write", "file_read"],
        "sentinel": "hello",
        "timeout_s": 300,
    },
    {
        "key": "no_skill_needed",
        "prompt": "用一个词回答：一天有多少小时？不要用任何工具。",
        "forbid_tools": ["load_skill", "code_run", "file_glob", "file_grep"],
        "sentinel": "24",
        "timeout_s": 240,
    },
    {
        "key": "honest_unknown",
        "prompt": "Hlaudz 3.0 的正式发布日期是哪天？查不到就直接说查不到，不许编一个日期。",
        # 本地探查（grep / glob）是合理动作，不禁；要抓的是「编一个日期」。
        "forbid_pattern": r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|20\d{2}-\d{2}-\d{2}",
        "must_match": r"查不到|无法(核实|查|确认)|没有(可靠|可查|找到)|不确定|不知道",
        "timeout_s": 240,
    },
]


async def _wait_health(base: str, timeout_s: float = 90.0) -> None:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(base_url=base, timeout=3) as c:
                r = await c.get("/api/health")
                if r.status_code < 400:
                    return
                last = f"status={r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}"
        await asyncio.sleep(0.7)
    raise RuntimeError(f"服务未就绪: {base} ({last})")


def _port_free(port: int) -> bool:
    """端口必须空着：否则会静默连上别的实例，两变体数据混在一起。"""
    import socket
    s = socket.socket()
    try:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def start_server(variant: str, port: int) -> subprocess.Popen:
    if not _port_free(port):
        raise RuntimeError(
            f"端口 {port} 已被占用——很可能上一次消融进程还在跑。"
            f"先结束它再重跑，否则两种提示词的数据会混在一起。")
    env = dict(os.environ)
    env["HLAUDZ_PROMPT_VARIANT"] = variant
    env["PYTHONUNBUFFERED"] = "1"
    log = HERE / "runs" / f"ablation-server-{variant}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env, stdout=fh, stderr=subprocess.STDOUT,
    )
    proc._hlz_log = fh  # type: ignore[attr-defined]
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    fh = getattr(proc, "_hlz_log", None)
    if fh:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass


def measure(case: dict, result: dict) -> dict:
    tools = result["tools"]
    text = result.get("text") or ""
    checks = result["checks"]
    tool_ok = all(c["ok"] for c in checks if c["kind"] in ("expect_tool", "forbid_tool"))
    must = case.get("must_match")
    if must:
        hit = bool(re.search(must, text))
        checks.append({"kind": "must_match", "name": must, "ok": hit})
        tool_ok = tool_ok and hit
    pattern = case.get("forbid_pattern")
    if pattern:
        checks.append({"kind": "forbid_pattern", "name": pattern,
                       "ok": not re.search(pattern, text)})
        tool_ok = tool_ok and not re.search(pattern, text)
    return {
        "key": case["key"],
        "tool_ok": tool_ok,
        "skill_load": tools.count("load_skill"),
        "rounds": len(tools),
        "code_run": tools.count("code_run"),
        "elapsed_s": result["elapsed_s"],
        "text_len": len(text),
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "run_id": result["run_id"],
        "text_tail": text[-400:],
    }


async def run_variant(variant: str, cases: list[dict], rounds: int) -> list[dict]:
    port = PORTS[variant]
    base = f"http://127.0.0.1:{port}"
    proc = start_server(variant, port)
    rows: list[dict] = []
    try:
        await _wait_health(base)
        harness.BASE = base
        for case in cases:
            for r in range(rounds):
                c = dict(case)
                c["name"] = f"ablation-{variant}-{case['key']}-{r + 1}"
                try:
                    result = await harness.run_case(c)
                    row = measure(case, result)
                except Exception as exc:  # noqa: BLE001
                    row = {"key": case["key"], "tool_ok": False, "skill_load": 0,
                           "rounds": 0, "code_run": 0, "elapsed_s": 0,
                           "text_len": 0, "ok": False, "run_id": "-",
                           "checks": [], "text_tail": f"{type(exc).__name__}: {exc}"}
                row["variant"] = variant
                row["round"] = r + 1
                rows.append(row)
                mark = "PASS" if row["ok"] else "FAIL"
                print(f"[{mark}] {variant}/{case['key']}#{r + 1} "
                      f"tools={row['rounds']} run={row['run_id']}", flush=True)
    finally:
        stop_server(proc)
    return rows


def aggregate(rows: list[dict]) -> dict:
    n = len(rows) or 1
    return {
        "runs": len(rows),
        "tool_ok_rate": round(sum(1 for r in rows if r["tool_ok"]) / n, 3),
        "pass_rate": round(sum(1 for r in rows if r["ok"]) / n, 3),
        "skill_load_total": sum(r["skill_load"] for r in rows),
        "avg_rounds": round(sum(r["rounds"] for r in rows) / n, 2),
        "avg_elapsed_s": round(sum(r["elapsed_s"] for r in rows) / n, 1),
        "avg_text_len": round(sum(r["text_len"] for r in rows) / n, 1),
    }


def write_report(out: Path, rows: list[dict]) -> Path:
    variants: dict[str, list[dict]] = {}
    for r in rows:
        variants.setdefault(r["variant"], []).append(r)
    agg = {v: aggregate(rs) for v, rs in variants.items()}
    (out / "report.json").write_text(
        json.dumps({"aggregate": agg, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    lines = ["# 提示词消融报告", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", "",
             "## 汇总", "",
             "| 变体 | 用例数 | 工具选择正确率 | 全通过率 | 无谓 skill 加载 | 平均轮次 | 平均耗时 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for v, a in agg.items():
        lines.append(f"| {v} | {a['runs']} | {a['tool_ok_rate']:.0%} | {a['pass_rate']:.0%} "
                     f"| {a['skill_load_total']} | {a['avg_rounds']} | {a['avg_elapsed_s']}s |")
    lines += ["", "## 逐用例", "",
              "| 变体 | 用例 | 轮次 | 工具 | 结果 | run |",
              "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['variant']} | {r['key']}#{r['round']} | {r['rounds']} "
                     f"| {'PASS' if r['tool_ok'] else 'FAIL'} "
                     f"| {'PASS' if r['ok'] else 'FAIL'} | {r['run_id']} |")
    lines += ["", "## 失败明细", ""]
    any_fail = False
    for r in rows:
        bad = [c for c in r["checks"] if not c["ok"]]
        if not bad and r["ok"]:
            continue
        any_fail = True
        lines.append(f"### {r['variant']} / {r['key']}#{r['round']} ({r['run_id']})")
        for c in bad:
            lines.append(f"- `{c['kind']}:{c['name']}` 不满足")
        if r.get("text_tail"):
            lines.append("")
            lines.append("```text")
            lines.append(r["text_tail"].replace("```", "'''"))
            lines.append("```")
        lines.append("")
    if not any_fail:
        lines.append("无")
    p = out / "report.md"
    p.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    return p


async def main_async(args: argparse.Namespace) -> int:
    cases = CASES
    if args.cases:
        want = {x.strip() for x in args.cases.split(",") if x.strip()}
        cases = [c for c in CASES if c["key"] in want]
        if not cases:
            print("没有匹配的用例:", sorted(c["key"] for c in CASES))
            return 2
    variants = [args.variant] if args.variant else ["new", "legacy"]
    rows: list[dict] = []
    for v in variants:
        print(f"== 变体 {v} ==", flush=True)
        rows += await run_variant(v, cases, args.rounds)
    out = HERE / "runs" / f"ablation-{int(time.time())}"
    out.mkdir(parents=True, exist_ok=True)
    path = write_report(out, rows)
    print("report:", path, flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["new", "legacy"], default=None)
    ap.add_argument("--cases", default="")
    ap.add_argument("--rounds", type=int, default=1)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

"""中等难度任务的真实 LLM E2E：验证沙箱建好之后动态工作流与 Agent Team 能跑通。

## 与之前测试的区别

`multiagent_llm_e2e.py` 测的是「机制通不通」（能不能 spawn、能不能执行）。
这里测的是**任务能不能完成**：给一个需要多步骤、需要写代码、需要验证的中等
难度任务，看端到端产出是否真的正确。

## 三个场景（由易到难）

| 场景 | 难度 | 考察什么 |
| --- | --- | --- |
| `wf` 动态工作流 | 中 | 多步骤流水线：生成数据 -> 处理 -> 验证 -> 报告 |
| `team` Agent Team | 中 | 多个 teammate 分工协作，各自产物汇总 |
| `both` 混合 | 中偏难 | 工作流里再派 teammate |

## 判定原则（不看模型自述）

- 产物文件是否**真的存在且内容正确**（落到 D:\\sandbox\\<uid>\\）
- 关键计算是否正确（用独立方法复核，不信模型说的）
- 中途是否有工具错误 / LLM 重试耗尽

用法：
    python tests/manual/midtask_e2e.py --case wf --user hx
    python tests/manual/midtask_e2e.py --case all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
RUNS = Path(__file__).resolve().parents[1] / "e2e" / "sandbox_runs"
USERS = {"hx": "u_c56b8337dd09", "xh": "u_343a46896bff"}
SANDBOX_ROOT = Path(r"D:\sandbox")


async def token_for(uid: str) -> str:
    from app import db
    from app.auth import create_access_token
    u = await db.get_user(uid)
    t, _ = create_access_token(uid, u.get("token_ver", 0))
    return t


def text_of(events: list[dict]) -> str:
    return "".join(e.get("text", "") for e in events if "text" in e)


def tool_blob(events: list[dict]) -> str:
    parts = []
    for e in events:
        if e.get("name") and "content" in e:
            c = e["content"]
            parts.append(f"{e['name']}: {c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)}")
    return "\n".join(parts)


# ---------------- 三个任务 ----------------

WF_TASK = """请用**动态工作流**完成一个数据分析任务。这个任务需要多步骤，请真正地用工作流编排，不要用单次 code_run 糊过去。

任务：分析一组销售数据。
1. 用 code_run 生成数据：创建 `sales_{sentinel}.csv`，500 行，列 = [date(2026-01-01 起), region(A/B/C/D), amount(随机 10~1000)]
2. 用 code_run 计算：每个 region 的总额与笔数，输出 `summary_{sentinel}.json`
3. 用 code_run 独立验证：用另一种方式重算一遍，确认与 summary.json 一致
4. 用 file_write 写 `report_{sentinel}.md`，包含各 region 的总额、笔数、占比

所有文件写在 workspace 根目录。最后告诉我：
- 哪个 region 总额最高、金额多少
- 验证是否通过

把 `{sentinel}` 这个标记写进 report.md 的末尾。
"""

TEAM_TASK = """请用 **Agent Team** 完成一个需要分工的任务。

任务：为一个字符串工具库做「实现 + 测试 + 审查」。
1. 派 3 个 teammate：
   - 一个负责实现：写 `strutil_{sentinel}.py`，包含 `is_palindrome(s)` 和 `word_count(s)` 两个函数
   - 一个负责测试：写 `test_strutil_{sentinel}.py`，用 code_run 跑通（覆盖回文、非回文、空串、中英混排）
   - 一个负责审查：读前两人产出的文件，指出至少一个问题或确认无误
2. 等三个都完成后，你自己再用 code_run 跑一次测试，确认全绿

要求：teammate 之间不要互相覆盖文件；测试必须真的执行过。
最后告诉我测试结果，以及审查发现了什么。

在最终报告里写上 `{sentinel}`。
"""

BOTH_TASK = """请先用**动态工作流**规划，其中至少一个节点用 **Agent Team** 派子代理完成。

任务：做一个简单的数据管道。
阶段1（你自己做）：用 code_run 生成 `raw_{sentinel}.txt`，内容为 200 个随机整数，空格分隔
阶段2（派 1 个 teammate 做）：读 `raw_{sentinel}.txt`，算出 最大值/最小值/平均值/中位数，写入 `stats_{sentinel}.json`
阶段3（你自己做）：读 `stats_{sentinel}.json`，用 code_run 独立复核这些数字（自己重算一遍），
                    一致则写 `verified_{sentinel}.md`，不一致则写差异

要求：
- 阶段2 必须真的派 teammate，不要自己做
- 阶段3 的复核必须是自己重算，不能只读 stats.json 就相信

最后报告：复核是否一致、stats.json 的实际内容。
在 verified.md 里写上 `{sentinel}`。
"""

CASES = {"wf": ("动态工作流", WF_TASK), "team": ("Agent Team", TEAM_TASK),
         "both": ("工作流+Team 混合", BOTH_TASK)}


async def run_one(client: httpx.AsyncClient, case_key: str, uid: str,
                  timeout_s: float) -> dict:
    label, tmpl = CASES[case_key]
    run_id = f"{int(time.time())}-{secrets.token_hex(2)}"
    sentinel = secrets.token_hex(4)
    out_dir = RUNS / f"mid-{case_key}-{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    token = await token_for(uid)
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    r = await client.post("/api/sessions", json={"title": f"mid-{case_key}-{run_id}"},
                          headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    sid = r.json()["id"]

    events: list[dict] = []
    t0 = time.time()
    async with client.stream("POST", f"/api/sessions/{sid}/messages",
                             json={"text": tmpl.format(sentinel=sentinel)},
                             headers=headers, timeout=timeout_s) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if raw in ("", "[DONE]"):
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    elapsed = time.time() - t0

    (out_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    tools = [e.get("name") for e in events if e.get("name") and "input" in e]
    tb = tool_blob(events)
    text = text_of(events)

    # LLM 侧故障信号
    llm_fail = ("连接中断" in text or "连接中断" in tb
                or "PoolTimeout" in tb or "重试仍失败" in tb)

    # 工具错误
    tool_errs = [ln for ln in tb.splitlines()
                 if "工具执行错误" in ln or "can't open file" in ln
                 or "路径越界" in ln or "No such file" in ln]

    result = {
        "label": label, "case": case_key, "uid": uid, "session": sid,
        "run_id": run_id, "sentinel": sentinel,
        "elapsed_s": round(elapsed, 1),
        "tools": tools, "n_tools": len(tools),
        "tool_errs": tool_errs,
        "llm_fail": llm_fail,
        "text_tail": text[-600:],
        "tool_blob_head": tb[:1500],
        "dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(
        json.dumps({k: v for k, v in result.items() if k != "tool_blob_head"},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ---------------- 独立复核（不看模型自述） ----------------

def verify_wf(uid: str, sentinel: str) -> list[tuple[str, bool, str]]:
    """复核工作流任务的产物。用 Python 独立重算，不信模型说的数字。"""
    import csv
    import json as _json

    root = SANDBOX_ROOT / uid
    checks: list[tuple[str, bool, str]] = []

    csv_path = root / f"sales_{sentinel}.csv"
    summary_path = root / f"summary_{sentinel}.json"
    report_path = root / f"report_{sentinel}.md"

    checks.append(("wf: sales.csv 存在", csv_path.exists(), str(csv_path)))
    checks.append(("wf: summary.json 存在", summary_path.exists(), str(summary_path)))
    checks.append(("wf: report.md 存在", report_path.exists(), str(report_path)))

    if csv_path.exists() and summary_path.exists():
        try:
            with csv_path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            # 独立重算
            agg: dict[str, float] = {}
            cnt: dict[str, int] = {}
            for row in rows:
                reg = row.get("region", "")
                amt = float(row.get("amount", 0))
                agg[reg] = agg.get(reg, 0.0) + amt
                cnt[reg] = cnt.get(reg, 0) + 1
            summary = _json.loads(summary_path.read_text(encoding="utf-8"))

            # summary 结构自由度很高，模型可能写成：
            #   {"regions": {"A": {"total":..}}}  /  {"A": {"total":..}}
            #   [{"region":"A","total":..}]      /  {"A": 65453}
            # 这里做一个「找出所有 region->金额」的宽松解析，
            # 只认键名是 A/B/C/D 且值可转成数字（或其下有 total/amount 字段）。
            got: dict[str, float] = {}

            def _absorb(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if isinstance(k, str) and k.strip().upper() in ("A", "B", "C", "D"):
                            if isinstance(v, dict):
                                num = v.get("total") or v.get("amount") or v.get("sum")
                                if num is not None:
                                    got[k.strip().upper()] = float(num)
                            elif isinstance(v, (int, float)):
                                got[k.strip().upper()] = float(v)
                        else:
                            _absorb(v)          # 往子字典里找（如 regions）
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict):
                            rk = (item.get("region") or item.get("name") or "")
                            if isinstance(rk, str) and rk.strip().upper() in ("A","B","C","D"):
                                num = item.get("total") or item.get("amount") or item.get("sum")
                                if num is not None:
                                    got[rk.strip().upper()] = float(num)
                            else:
                                _absorb(item)

            _absorb(summary)

            if got and agg:
                diffs = {k: abs(got.get(k, 0) - agg[k]) for k in agg}
                max_diff = max(diffs.values()) if diffs else 1e9
                checks.append(("wf: summary 金额与独立重算一致",
                               max_diff < 1.0,
                               f"最大偏差 {max_diff:.2f}（区域 {sorted(agg)}）"))
            else:
                checks.append(("wf: summary 金额与独立重算一致", False,
                               f"结构无法解析: {str(summary)[:120]}"))
            checks.append(("wf: 数据行数 500", len(rows) == 500, f"{len(rows)} 行"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("wf: 产物可解析", False, f"{type(exc).__name__}: {exc}"))

    if report_path.exists():
        checks.append(("wf: report.md 含哨兵",
                       sentinel in report_path.read_text(encoding="utf-8", errors="replace"),
                       sentinel))
    return checks


def verify_team(uid: str, sentinel: str) -> list[tuple[str, bool, str]]:
    root = SANDBOX_ROOT / uid
    checks: list[tuple[str, bool, str]] = []
    for name in (f"strutil_{sentinel}.py", f"test_strutil_{sentinel}.py"):
        p = root / name
        checks.append((f"team: {name} 存在", p.exists(), str(p)))

    su = root / f"strutil_{sentinel}.py"
    if su.exists():
        src = su.read_text(encoding="utf-8", errors="replace")
        checks.append(("team: strutil.py 有两个函数",
                       "is_palindrome" in src and "word_count" in src, ""))
        # 独立复核实现是否正确
        try:
            ns: dict = {}
            exec(compile(src, str(su), "exec"), ns)
            fn = ns.get("is_palindrome")
            if callable(fn):
                cases = [("aba", True), ("abc", False), ("", True),
                         ("A man a plan", False), ("上海自来水来自海上", True)]
                ok = all(bool(fn(s)) == want for s, want in cases)
                checks.append(("team: is_palindrome 行为正确", ok, str(cases)))
            else:
                checks.append(("team: is_palindrome 行为正确", False, "不是函数"))
            wc = ns.get("word_count")
            if callable(wc):
                ok2 = wc("hello world foo") == 3
                checks.append(("team: word_count 行为正确", ok2,
                               repr(wc("hello world foo"))))
        except Exception as exc:  # noqa: BLE001
            checks.append(("team: strutil.py 可执行", False,
                           f"{type(exc).__name__}: {exc}"))
    return checks


def verify_both(uid: str, sentinel: str) -> list[tuple[str, bool, str]]:
    import json as _json
    root = SANDBOX_ROOT / uid
    checks: list[tuple[str, bool, str]] = []
    for name in (f"raw_{sentinel}.txt", f"stats_{sentinel}.json", f"verified_{sentinel}.md"):
        p = root / name
        checks.append((f"both: {name} 存在", p.exists(), str(p)))

    raw = root / f"raw_{sentinel}.txt"
    stats = root / f"stats_{sentinel}.json"
    if raw.exists() and stats.exists():
        try:
            nums = [int(x) for x in raw.read_text(encoding="utf-8").split()]
            checks.append(("both: raw.txt 含 200 个整数", len(nums) == 200, f"{len(nums)} 个"))
            s = _json.loads(stats.read_text(encoding="utf-8"))
            def pick(d, *keys):
                for k in keys:
                    if k in d:
                        return float(d[k])
                return None
            got_max, got_min = pick(s, "max", "maximum"), pick(s, "min", "minimum")
            got_avg = pick(s, "avg", "mean", "average")
            exp_max, exp_min = max(nums), min(nums)
            exp_avg = sum(nums) / len(nums)
            checks.append(("both: 最大值正确", got_max is not None and abs(got_max - exp_max) < 1e-6,
                           f"got {got_max} want {exp_max}"))
            checks.append(("both: 最小值正确", got_min is not None and abs(got_min - exp_min) < 1e-6,
                           f"got {got_min} want {exp_min}"))
            checks.append(("both: 平均值正确",
                           got_avg is not None and abs(got_avg - exp_avg) < 0.01,
                           f"got {got_avg} want {exp_avg:.2f}"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("both: 产物可解析", False, f"{type(exc).__name__}: {exc}"))

    vm = root / f"verified_{sentinel}.md"
    if vm.exists():
        checks.append(("both: verified.md 含哨兵",
                       sentinel in vm.read_text(encoding="utf-8", errors="replace"), sentinel))
    return checks


VERIFIERS = {"wf": verify_wf, "team": verify_team, "both": verify_both}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="wf", choices=["wf", "team", "both", "all"])
    ap.add_argument("--user", default="hx", choices=list(USERS))
    ap.add_argument("--timeout", type=float, default=600)
    a = ap.parse_args()

    which = ["wf", "team", "both"] if a.case == "all" else [a.case]
    uid = USERS[a.user]

    results = []
    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        try:
            h = await client.get("/api/health")
            h.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"后端不可用：{exc}")
            return 2

        for key in which:
            print(f"\n{'='*60}\n任务：{CASES[key][0]}  user={a.user}  uid={uid}\n{'='*60}")
            r = await run_one(client, key, uid, a.timeout)
            results.append(r)
            print(f"  耗时 {r['elapsed_s']}s  工具 {r['n_tools']} 次")
            print(f"  工具序列: {r['tools'][:15]}")
            if r["llm_fail"]:
                print("  ⚠️ LLM 侧出现故障")
            if r["tool_errs"]:
                print(f"  ⚠️ 工具错误 {len(r['tool_errs'])} 条")

    # ---------------- 判定 ----------------
    print(f"\n{'='*60}\n判定\n{'='*60}")
    all_checks: list[tuple[str, bool, str]] = []
    for r in results:
        all_checks.append((f"{r['case']}: 无工具错误", not r["tool_errs"],
                           "; ".join(r["tool_errs"])[:100]))
        all_checks.append((f"{r['case']}: LLM 侧无故障", not r["llm_fail"], ""))
        all_checks.extend(VERIFIERS[r["case"]](r["uid"], r["sentinel"]))

    bad = 0
    for name, ok, detail in all_checks:
        if not ok:
            bad += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    print(f"\n===== {len(all_checks)-bad}/{len(all_checks)} 通过 =====")
    for r in results:
        print(f"  现场: {r['dir']}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
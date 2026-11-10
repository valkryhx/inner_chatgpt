"""跑 Hlaudz e2e 场景：python -m tests.e2e.run --case S1[,S2]"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys

from .harness import run_case

SENTINEL = "SENTINEL=" + secrets.token_hex(3)


def cases() -> dict[str, dict]:
    return {
        "S0": {
            "name": "negative-control",
            "prompt": "今天几号？直接回答，不要调用任何搜索或派发工具。",
            "forbid_tools": ["spawn_agent", "spawn_teammate", "file_grep", "file_glob"],
            "timeout_s": 150,
        },
        "S8": {
            "name": "tool-preference",
            "prompt": "在当前工作区里找出所有包含 TODO 的地方并统计数量，只要结果和文件列表。",
            "expect_tools": ["file_grep"],
            "forbid_tools": ["code_run"],
            "timeout_s": 240,
        },
        "S1": {
            "name": "subagent-basic",
            "prompt": (f"派一个子代理去计算 17*23，并要求它在结果里原样复述这个字符串：{SENTINEL}。"
                       "你自己不要算，等它完成后把结果告诉我。"),
            "expect_tools": ["spawn_agent", "wait_agent"],
            "sentinel": SENTINEL,
            "timeout_s": 420,
        },
        "S2": {
            "name": "subagent-parallel",
            "prompt": (f"同时派 3 个子代理，分别计算 11*11、12*12、13*13。"
                       f"每个子代理的结果里都要原样包含这个字符串：{SENTINEL}。"
                       "三个一起派，等全部完成后汇总三个结果。"),
            "expect_tools": ["spawn_agent", "wait_agent", "read_agent_result"],
            "tool_counts": {"spawn_agent": 3},
            "sentinel": SENTINEL,
            "timeout_s": 480,
        },
        "S7": {
            "name": "subagent-cancel",
            "prompt": ("派 2 个子代理：first 计算 5*5，second 写一首 4 行的诗。"
                       "派完立刻用 close_agent 关掉 second（不要等它），"
                       "然后等 first 完成，把 first 的结果告诉我。"),
            "expect_tools": ["spawn_agent", "close_agent", "wait_agent"],
            "tool_counts": {"spawn_agent": 2},
            "timeout_s": 420,
        },
        "S4": {
            "name": "subagent-steer",
            "prompt": ("派一个子代理统计 backend 目录下有多少个 .py 文件。"
                       "派完立刻用 followup_task 追加一条要求：再统计 .ts 文件数量。"
                       "然后等它完成，汇报两个数字。"),
            "expect_tools": ["spawn_agent", "followup_task", "wait_agent"],
            "timeout_s": 480,
        },
        "S3": {
            "name": "subagent-timeout",
            "prompt": ("派一个子代理，让它用 code_run 跑一段 Python：先睡眠 75 秒，"
                       f"然后打印 SLEEP DONE {SENTINEL}。派完后先用 wait_agent 等 15 秒"
                       "（会超时，这是预期的，不要因此重新 spawn）；确认它还在运行后，"
                       "再 wait_agent 直到它完成，然后读取结果并告诉我。"),
            "expect_tools": ["spawn_agent", "wait_agent", "read_agent_result"],
            "tool_counts": {"spawn_agent": 1},
            "sentinel": SENTINEL,
            "timeout_s": 420,
        },
        "S6": {
            "name": "team-scale",
            "prompt": ("建一个团队任务板，创建 13 个任务：10 个简单任务（每个用一句话介绍一个"
                       "中国城市，城市自选且不重复），3 个中等任务（分别写一段 80 字左右的杭州、"
                       "成都、西安介绍）。派 2 个 teammate 完成这些任务，全部结束后汇总"
                       "任务板状态（每个任务 id 与最终状态）。"),
            "expect_tools": ["task_create", "spawn_teammate", "task_list"],
            "tool_min_counts": {"task_create": 13, "spawn_teammate": 2},
            "expect_all_tasks_completed": True,
            "min_tasks": 13,
            "timeout_s": 900,
        },
        "S9": {
            "name": "subagent-slot-limit",
            "prompt": ("在**同一轮**里一次性发出 6 个 spawn_agent（分别计算 1*1 到 6*6），"
                       "每个子代理都必须先用 code_run 睡眠 8 秒再计算，否则会太早结束、测不到上限。"
                       "不要等前面的完成再派后面的。每个子代理结果里都要原样包含："
                       f"{SENTINEL}。如果某个 spawn 被拒绝（例如「已达上限」），"
                       "把错误原文复述给我，然后 close 已完成的再补派，最后汇总 6 个结果。"),
            "expect_tools": ["spawn_agent", "close_agent"],
            "tool_min_counts": {"spawn_agent": 4},
            "sentinel": SENTINEL,
            "timeout_s": 600,
        },
        "S10": {
            "name": "subagent-long-task",
            "prompt": ("派一个子代理，让它用 code_run 跑 Python：先 time.sleep(660)，"
                       f"然后打印 LONG DONE {SENTINEL}。派完先用 wait_agent 等 180 秒"
                       "（会超时，这完全正常，绝对不要重新 spawn）；之后继续 wait_agent "
                       "直到它完成，再读取结果并告诉我。整个任务大约需要 11 分钟。"),
            "expect_tools": ["spawn_agent", "wait_agent", "read_agent_result"],
            "tool_counts": {"spawn_agent": 1},
            "sentinel": SENTINEL,
            "timeout_s": 1500,
        },
        "S5": {
            "name": "team-basic",
            "prompt": ("建一个 2 个任务的团队任务板：任务 A 写一句话介绍杭州，任务 B 写一句话介绍成都。"
                       "派 1 个 teammate 依次完成这两个任务，最后汇总。"),
            "expect_tools": ["task_create", "spawn_teammate"],
            "timeout_s": 600,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="S0", help="场景 id，逗号分隔")
    args = ap.parse_args()
    table = cases()
    names = [n.strip() for n in args.case.split(",") if n.strip()]
    failed = 0
    for name in names:
        case = table.get(name)
        if not case:
            print(f"[skip] 未知场景 {name}")
            failed += 1
            continue
        result = asyncio.run(run_case(case))
        mark = "PASS" if result["ok"] else "FAIL"
        print(f"[{mark}] {name} {result['case']} runId={result['run_id']} "
              f"{result['elapsed_s']}s tools={result['tools']}")
        bad = [c for c in result["checks"] if not c["ok"]]
        for c in bad:
            print(f"   FAILED {c['kind']}: {c.get('name')}")
        print(f"   dir: {result['dir']}")
        failed += 0 if result["ok"] else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

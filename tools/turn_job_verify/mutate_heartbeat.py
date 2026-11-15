"""变异测试：心跳 + 生命周期收口 + 代际栅栏。

每条变异跑一次**相关测试子集**，期望它们变红。漏网说明测试测不出东西 ——
阶段 2 已经吃过一次这个亏（`work/_mutate_job2.py` 里 N4/N6 是我自己写错的变异），
所以这里的每条变异都必须**真的改变行为**，不能是等价改写。
"""
import pathlib
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[2]
TJ = ROOT / "backend" / "app" / "turn_job.py"
ORIG = TJ.read_text(encoding="utf-8")

HEARTBEAT_DEF = '_HEARTBEAT_FRAME = ": keep-alive\\n\\n"'
IDLE_LINE = "idle: Optional[float] = HEARTBEAT_IDLE_S if HEARTBEAT_IDLE_S > 0 else None"
WAIT_LINE = "item = await asyncio.wait_for(self.queue.get(), idle)"
TIMEOUT_BRANCH = "                    yield _HEARTBEAT_FRAME\n                    continue"
REAP_BLOCK = (
    "    def _reap(_t: asyncio.Task, _job: \"TurnJob\" = job) -> None:\n"
    "        _job.finish()\n"
    "        _job.task = None   # 断开 job <-> task 的引用环，不然要靠 gc 回收\n"
    "\n"
    "    task.add_done_callback(_reap)\n"
)
NEWEST_LINE = "        if best is None or job.started_at > best.started_at:\n            best = job"
SKIP_FINISHED = "        if uid != str(user_id) or sid != str(session_id) or job.finished:\n            continue"

MUTATIONS = [
    # ---- 心跳 ----
    ("H1 心跳永不发（idle 置 None）",
     IDLE_LINE,
     "idle: Optional[float] = None",
     "test_heartbeat_fires_when_idle"),

    ("H2 心跳间隔不看空闲（固定 1ms）",
     WAIT_LINE,
     "item = await asyncio.wait_for(self.queue.get(), 0.001)",
     "test_heartbeat_silent_while_busy"),

    # Subscriber 没有 job 反向引用，drain 内部碰不到 job.seq —— 所以「推进 seq」这条
    # 路径结构上不可能。改测另一个真实危险：心跳帧若带上 id:，就会污染续看游标。
    ("H3 心跳帧带上 id:（污染续看游标）",
     HEARTBEAT_DEF,
     '_HEARTBEAT_FRAME = "id: 999\\n: keep-alive\\n\\n"',
     "test_heartbeat_does_not_advance_seq_or_enter_replay"),

    # 「超时后 get_nowait 清队列」是等价无害的：超时那一刻队列本来就是空的
    # （否则 get 会立刻返回，不会超时）。真危险的是「超时后不 continue」——
    # 把超时误当成「流结束」，后续真实帧全丢（实测 5 帧 → 0 帧）。
    ("H4 超时后不 continue（误当流结束，丢后续帧）",
     TIMEOUT_BRANCH,
     "                    yield _HEARTBEAT_FRAME\n                    return",
     "test_heartbeat_interleaved_does_not_lose_real_frames"),

    ("H5 关不掉心跳（把 >0 改成 >=0）",
     IDLE_LINE,
     "idle: Optional[float] = HEARTBEAT_IDLE_S if HEARTBEAT_IDLE_S >= 0 else None",
     "test_heartbeat_can_be_disabled"),

    # ---- 生命周期收口 ----
    ("Z1 去掉 done_callback 兜底收口",
     REAP_BLOCK,
     "",
     "test_job_finishes_even_if_cancelled_before_first_await"),

    # ---- 代际栅栏 ----
    ("G1 取第一个而不是最新一代",
     NEWEST_LINE,
     "        if best is None:\n            best = job",
     "test_live_job_picks_newest_generation"),

    ("G2 不跳过已结束的 job",
     SKIP_FINISHED,
     "        if uid != str(user_id) or sid != str(session_id):\n            continue",
     "test_live_job_skips_finished"),
]


def run_one(node_id: str) -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", node_id, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT / "backend"), capture_output=True, text=True, timeout=600)
    return r.returncode != 0, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    caught = missed = 0
    for name, before, after, expect_test in MUTATIONS:
        if before not in ORIG:
            print(f"  [SKIP] {name} —— 锚点没匹配上（变异本身写错了，不是测试的问题）")
            missed += 1
            continue
        TJ.write_text(ORIG.replace(before, after, 1), encoding="utf-8", newline="\n")
        try:
            failed, out = run_one(f"tests/test_turn_job.py::{expect_test}")
        finally:
            TJ.write_text(ORIG, encoding="utf-8", newline="\n")
        if failed:
            caught += 1
            why = next(
                (ln.strip() for ln in out.splitlines()
                 if ln.strip().startswith(("assert", "E ", "AssertionError"))), "")
            print(f"  [CAUGHT] {name}" + (f"  | {why[:64]}" if why else ""))
        else:
            missed += 1
            print(f"  [MISSED] {name}  ← 期望 {expect_test} 变红却没红")

    TJ.write_text(ORIG, encoding="utf-8", newline="\n")
    print(f"\n结果：{caught} 被抓 / {missed} 漏网")
    return 0 if missed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

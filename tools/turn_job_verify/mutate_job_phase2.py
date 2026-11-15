"""阶段 2 变异测试：把重放/sync 的逻辑改坏，确认测试真能抓到。

用法：cd backend && python ../tools/turn_job_verify/mutate_job2.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

MUTATIONS = [
    (
        "N1 seq 不重写（用外部 id）",
        "app/turn_job.py",
        "        frame = self._rewrite_seq(frame, self.seq)\n",
        "",
    ),
    (
        "N2 seq 不递增（永远是 0）",
        "app/turn_job.py",
        "        self.seq += 1\n",
        "",
    ),
    (
        "N3 窗口判定写错（±1 偏）",
        "app/turn_job.py",
        "        if after + 1 >= self.oldest_seq():\n",
        "        if after >= self.oldest_seq():\n",
    ),
    (
        "N4 sync 用缓存快照而非实时 blocks",
        "app/turn_job.py",
        "        self.blocks_provider = provider\n",
        "        self.blocks_provider = (lambda _c=[dict(b) for b in provider()]: list(_c))\n",
    ),
    (
        "N5 缓冲不淘汰（无上限）",
        "app/turn_job.py",
        "        while (len(self.replay) > REPLAY_MAX_FRAMES\n"
        "               or self.replay_bytes > REPLAY_MAX_BYTES):\n",
        "        while False:\n",
    ),
    (
        "N6 窗口内也走 sync（不重复但白传快照）",
        "app/turn_job.py",
        "        if after + 1 >= self.oldest_seq():\n",
        "        if False:\n",
    ),
    (
        "N7 重放时多发一帧（off-by-one）",
        "app/turn_job.py",
        "            return [f for s, f in self.replay if s > after]\n",
        "            return [f for s, f in self.replay if s >= after]\n",
    ),
    (
        "N8 续看端点抢租约（变成新开一轮）",
        "app/routers/chat.py",
        "    job = turn_job.get_live_job_for_session(user[\"id\"], session_id)\n",
        "    await team_store.acquire_generation(user[\"id\"], session_id, \"g_resume\")\n"
        "    job = turn_job.get_live_job_for_session(user[\"id\"], session_id)\n",
    ),
    (
        "N9 job 不传进 _event_stream（provider 装不上）",
        "app/routers/chat.py",
        "                    job=j,\n",
        "",
    ),
]


def main() -> int:
    missed = 0
    for name, path, old, new in MUTATIONS:
        p = pathlib.Path(path)
        orig = p.read_text(encoding="utf-8", newline="")
        if old not in orig:
            print(f"SKIP    {name}  (锚点未找到)")
            continue
        p.write_text(orig.replace(old, new, 1), encoding="utf-8", newline="")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/test_turn_job.py",
                 "-q", "--no-header", "-p", "no:cacheprovider"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            line = ""
            for ln in r.stdout.splitlines():
                if "passed" in ln or "failed" in ln:
                    line = ln.strip()
            caught = r.returncode != 0
            print(f"{'CAUGHT ' if caught else 'MISSED!'} {name}  -> {line}")
            if not caught:
                missed += 1
        finally:
            p.write_text(orig, encoding="utf-8", newline="")
    print()
    print(f"未被抓到的变异：{missed}/{len(MUTATIONS)}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

"""阶段 1 变异测试：把实现改坏，确认 10 条测试真能抓到。

用法：cd backend && python ../tools/turn_job_verify/mutate_job.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

MUTATIONS = [
    (
        "M1 detach 顺手取消 job（回到旧行为）",
        "app/turn_job.py",
        '    def detach(self, sub: Subscriber) -> None:\n'
        '        """订阅者离开（正常结束或客户端断开）。**不影响 job**。"""\n',
        '    def detach(self, sub: Subscriber) -> None:\n'
        '        """MUTATED"""\n'
        '        if self.task is not None and not self.task.done():\n'
        '            self.task.cancel()\n',
    ),
    (
        "M2 register_generation 挪到 job 外（请求任务）",
        "app/routers/chat.py",
        '        async def _run_job(j: turn_job.TurnJob) -> None:\n'
        '            # 必须在 **job 任务内部**登记：cancel_generation 读的是\n'
        '            # _generation_tasks[key]，只有登记成 job 任务，/stop 才能取消到\n'
        '            # 真正在跑的那个任务（登记成请求任务的话，请求一返回就取消不到了）。\n'
        '            stop_event = register_generation(user["id"], session_id, generation_id)\n',
        '        stop_event = register_generation(user["id"], session_id, generation_id)\n'
        '        async def _run_job(j: turn_job.TurnJob) -> None:\n',
    ),
    (
        "M3 无订阅者时 publish 抛错",
        "app/turn_job.py",
        "        self.frame_count += 1\n"
        "        for sub in list(self.subscribers):\n"
        "            sub.push(frame)\n",
        "        self.frame_count += 1\n"
        "        if not self.subscribers:\n"
        "            raise RuntimeError('no subscriber')\n"
        "        for sub in list(self.subscribers):\n"
        "            sub.push(frame)\n",
    ),
    (
        "M4 finish 不摘表",
        "app/turn_job.py",
        "        _JOBS.pop(_key(self.user_id, self.session_id, self.generation_id), None)\n",
        "",
    ),
    (
        "M5 订阅者上限去掉",
        "app/turn_job.py",
        "        if len(self.subscribers) >= MAX_SUBSCRIBERS:\n            return None\n",
        "",
    ),
    (
        "M6 溢出时连累其它订阅者（都收不到后续帧）",
        "app/turn_job.py",
        "        self.frame_count += 1\n"
        "        for sub in list(self.subscribers):\n"
        "            sub.push(frame)\n",
        "        self.frame_count += 1\n"
        "        if any(s.overflow for s in self.subscribers):\n"
        "            return   # MUTATED: 有一个溢出就不再给别人发\n"
        "        for sub in list(self.subscribers):\n"
        "            sub.push(frame)\n",
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

"""为什么 `os.kill(pid, 0)` 不能当进程存活判据（Windows）。

## 背景

`backend/tests/test_process_registry.py::test_code_run_stop_kills_child` 偶发失败：
单独跑 20/20 通过，全量跑偶发 `Failed: child process still alive after stop`。
根因不是产品 bug，是**判据会假阳**。

## 现场

在全量测试失败的那一刻抓现场（矩阵探针），同一个 pid 上三个判据：

| 判据 | 结果 |
|---|---|
| `os.kill(pid, 0)` | **alive**（假阳） |
| `tasklist /FI "PID eq <pid>"` | dead（真） |
| `psutil.pid_exists(pid)` | dead（真） |

50/50 轮三者不一致。进程**确实已经死了**（tasklist 与 psutil 一致），
只有 `os.kill` 谎报。

## 机制

Windows 的「进程对象」在进程退出后并不立刻销毁：只要系统里还有人持句柄，
那个 pid 就仍「存在」。`os.kill(pid, 0)` 走的是 `OpenProcess`，它检验的是
「能不能拿到这个进程对象」，不是「它是否还在运行」。句柄何时全部释放不受
我们控制（别的测试、subprocess 的清理时机都会影响）。

**但要小心别过度断言。** 在真实 `_run_code_impl` 路径下单跑时，`os.kill`
通常**正确**报死（`OSError winerror=87`，8/8 实测）—— 因为那时句柄已释放。
所以它不是「恒错」，而是「会假阳」。这正是这条用例「单跑绿、全量偶发红」
的原因：全量跑时机器上还有别的 Python 子进程占着 pid 空间与句柄。

## 结论

- Windows：用 `tasklist`（系统自带，无需新依赖）作权威判据；
- 类 Unix：`os.kill(pid, 0)` 语义确实是探活（`ESRCH` = 不存在），保留；
- `psutil.pid_exists` 也正确，但 psutil **不在本项目 requirements 里**
  （环境里的 5.9.0 属于巧合），不为一条测试引入未声明依赖。

## 跑法

    python tools/turn_job_verify/pid_alive_check.py

全部 PASS 退出码 0。其中「os.kill 假阳」那条是**期望成立**的 —— 它若变红，
说明平台行为变了，`pid_alive` 的实现应当重新审视。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""))


def tasklist_alive(pid: int) -> bool:
    r = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                       capture_output=True, text=True, timeout=20)
    return str(int(pid)) in (r.stdout or "")


def oskill_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def main() -> int:
    print("=" * 72)
    print("pid 存活判据核验（Windows）")
    print("=" * 72)

    # 两个判据对活进程都应说活
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    time.sleep(0.4)
    record("活进程：tasklist 说活", tasklist_alive(p.pid))
    record("活进程：os.kill 说活", oskill_alive(p.pid))
    p.kill()
    p.wait()
    time.sleep(0.2)

    # 核心：**不 wait 的**子进程被杀后，os.kill 假阳
    # （这是生产路径里句柄未及时释放的形态）
    q = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.4)
    subprocess.run(["taskkill", "/PID", str(q.pid), "/T", "/F"], capture_output=True)
    time.sleep(0.3)
    tl, ok_ = tasklist_alive(q.pid), oskill_alive(q.pid)
    record("taskkill 后 tasklist 说死", not tl, "权威判据")
    if os.name == "nt":
        record("taskkill 后 os.kill 假阳（这正是不能用它的原因）", ok_,
               "若变红说明平台行为变了，需复核 pid_alive")
    q.wait()

    # 不存在的 pid：两者都说死
    record("不存在 pid：tasklist 说死", not tasklist_alive(999999))
    record("不存在 pid：os.kill 说死", not oskill_alive(999999))

    # tasklist 判据在真实 taskkill 路径上稳定（50 轮）
    bad = 0
    for _ in range(50):
        r = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        time.sleep(0.15)
        subprocess.run(["taskkill", "/PID", str(r.pid), "/T", "/F"], capture_output=True)
        time.sleep(0.1)
        if tasklist_alive(r.pid):
            bad += 1
        r.wait()
    record("50 轮 taskkill 后 tasklist 判活数", bad == 0, f"{bad}/50（应为 0）")

    failed = [n for n, ok, _ in results if not ok]
    print()
    print("=" * 72)
    print(f"通过 {len(results) - len(failed)} / 失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

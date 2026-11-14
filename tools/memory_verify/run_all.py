"""记忆验证工具组的共用约定与入口。

四个脚本各自独立可跑（都有自己的 `main()`），这个文件只做两件事：

1. 说明这一组**测什么、为什么单测测不到**；
2. 提供一个统一的 `python tools/memory_verify/run_all.py` 入口。

| 脚本 | 层 | 证明什么 | 耗时 |
|---|---|---|---|
| `ui_dream.py` | **L3（用户能看到）** | 设置页「我的记忆」tab 的渲染、交互、不白屏 | ~90s |
| `concurrent_write.py` | **L2.5（进程内并发）** | dream 与提取同时写同一批文件时数据会不会丢 | ~5s |
| `multi_process_lock.py` | **L2.5（跨进程）** | DB 行锁在真跨进程下真的互斥 | ~25s |
| `selector_bench.py` | **L3（真模型收益）** | 选择器相对关键词的命中率与首帧开销（S-M6 / S-M7） | ~3min |
| `dream_mutation.py` | **L1.5（变异）** | 把 dream 的关键机制逐个改坏，验证有测试真的盯着它 | ~2min |
| `prefetch_mutation.py` | **L1.5（变异）** | 同上，对象是 M3.5 预取的 13 条机制 | ~2min |
| `run_all.py` | — | 依次跑前三个（变异/基准较慢，单独跑） | ~2min |

## 为什么这三件事必须单独测（变异测试见 `dream_mutation.py`）

`backend/tests/` 的 1012 项单测覆盖的是**判定逻辑**：

- 闸门函数给定输入返回什么；
- 锁的 SQL 在单进程、无竞争时行为正确；
- API 返回的形状。

它们**原理上**测不到三类东西：

1. **渲染**：`npm run build` 通过 != 页面能用。组件可能挂在 `undefined` 上、
   CSS 可能把按钮挤出可视区、tab 切换可能白屏。只有真浏览器能证伪。
2. **真并发**：单测里的「并发」通常是 `asyncio.gather` 两个协程，
   共享同一个事件循环和同一个 `aiosqlite` 连接池行为。而真实竞态发生在
   **两个写者各自走完整路径**时 —— 中间每一步的 `await` 都是换手点。
3. **跨进程**：`acquire_lock` 的原子性只在两个**独立进程**同时执行
   `UPDATE ... WHERE` 时才真正被检验。同进程两个协程连 SQLite 事务的
   锁升级路径都走不到。

## 关于「多 worker」

`backend/run.py` 是**单 worker**（`uvicorn.run()` 未传 `workers=`），
当前部署形态里不存在多 worker。所以 `multi_process_lock.py` 验证的是
**跨进程锁语义** —— 这才是「多 worker 会不会双跑 dream」的实质问题，
且比启动 uvicorn 多 worker 更可控（能精确控制时序、不受 Windows spawn 干扰）。

## 前置

```bash
pip install playwright && python -m playwright install chromium
cd frontend && npm run build      # ui_dream.py 需要最新的 dist
```

`ui_dream.py` / `concurrent_write.py` / `multi_process_lock.py` 都**不碰生产库**：
自己复制 `backend/data/app.db` 到 `work/` 下并用独立端口起后端。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

SCRIPTS = [
    ("concurrent_write.py", "进程内并发（dream x 提取）"),
    ("multi_process_lock.py", "跨进程锁互斥"),
    ("ui_dream.py", "Playwright UI（L3）"),
]


def main() -> int:
    only = sys.argv[1:]
    failed: list[str] = []
    for name, desc in SCRIPTS:
        if only and not any(o in name for o in only):
            continue
        print(f"\n{'=' * 60}\n{name} —— {desc}\n{'=' * 60}")
        started = time.time()
        r = subprocess.run([sys.executable, str(HERE / name)], cwd=str(ROOT),
                           text=True, encoding="utf-8", errors="replace")
        elapsed = time.time() - started
        print(f"--> {'PASS' if r.returncode == 0 else 'FAIL'} ({elapsed:.1f}s)")
        if r.returncode != 0:
            failed.append(name)

    print(f"\n{'=' * 60}")
    if failed:
        print(f"{len(failed)} 个脚本失败：{failed}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# 修复 `test_code_run_stop_kills_child` 偶发失败（判据假阳）

> 日期：2026-11-15　状态：**已修复**
> 前序：`docs/20261115-持久化第三步阶段4实施记录.md`（该记录里我把这条定性为「既有负载敏感假失败」，**那个定性是错的**）

---

## 0. 一句话

不是负载敏感，是**测试判据 `os.kill(pid, 0)` 在 Windows 上会假阳**：全量跑时它对着一个
已经死掉的 pid 报「还活着」，于是 `else: pytest.fail()` 误触发。

---

## 1. 我先前错在哪

阶段 4 记录 §4.4 写的是：

> `test_process_registry.py::test_code_run_stop_kills_child` 是**既有的负载敏感假失败**，
> 与本轮改动无关

两条都对了一半：

- 「与本轮改动无关」——对。基线也复现过。
- 「负载敏感」——**错**。它跟机器忙不忙无关，跟**句柄何时被释放**有关。

我当时给的证据里还有一句「单独跑 20/20 通过」，用它推出「所以是负载问题」。这个推理
本身就漏了一种可能：**单独跑和全量跑的系统状态不同，不是因为忙，而是因为机器上
还有别的进程持着句柄**。

---

## 2. 定位过程（含两次自我纠正）

### 2.1 第一次纠正：不是「恒误判」

我最早的裸脚本探针得出「Windows 上 `os.kill(pid, 0)` 40/40、200/200 全部误判为活着」。
**这个结论过头了。**

真实 `_run_code_impl` 路径下单独跑时，`os.kill` 通常**正确**报死
（`OSError winerror=87`，8/8 实测）。所以它不是恒错，而是**会假阳**——只在句柄
未释放时才错。

我那些 200/200 的裸脚本一直持着 `Popen` 引用没放，等于自己制造了假阳条件，
却当成「平台普遍行为」。这是探针构造污染结论。

### 2.2 决定性证据：全量失败那一刻的现场

写了一个矩阵探针（同时记三个判据），塞进 `tests/` 跑全量。失败时抓到：

```
pid=17924 轮数=50
最终: J1(os.kill)=True  J2(tasklist)=False  J3(psutil)=False
三者不一致的轮次数：50
前 3 个不一致样本：[(0, True, False, False), (1, True, False, False), (2, True, False, False)]
```

**50/50 轮全部不一致**，且 `tasklist` 与 `psutil` 两个独立判据一致说「死」。
进程真的死了，是 `os.kill` 谎报。

同时这次的失败输出也确认了失败点就是那句 `else: pytest.fail()`：

```
>           pytest.fail("child process still alive after stop")
E           Failed: child process still alive after stop
tests\test_process_registry.py:46: Failed
```

### 2.3 机制

Windows 的「进程对象」在进程退出后不立刻销毁：只要系统里还有人持句柄，那个 pid 就仍
「存在」。`os.kill(pid, 0)` 走的是 `OpenProcess`，检验的是「能不能拿到这个进程对象」，
不是「它是否还在运行」。

单跑时句柄恰好已释放 → `os.kill` 抛 `winerror=87` → 测试通过（所以 20/20 绿）；
全量跑时机器上还有别的 Python 子进程，pid 空间与句柄状态不同 → `os.kill` 假阳 →
测试误报。

**这也解释了为什么它「偶发」而不「必挂」**：取决于那一刻句柄有没有全部释放。

---

## 3. 修复

`backend/tests/test_process_registry.py` 加 `pid_alive()` helper：

```python
def pid_alive(pid: int) -> bool:
    pid = int(pid)
    if os.name == "nt":
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True, timeout=15)
        except Exception:
            return True      # tasklist 都起不来时保守判「还活着」
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
```

三个选择理由：

1. **Windows 用 tasklist** —— 系统自带，不需要新依赖；
2. **类 Unix 保留 `os.kill(pid, 0)`** —— 那里它的语义确实就是探活（`ESRCH` = 不存在），
   是标准做法，不能一起改掉；
3. **不用 psutil** —— 它判得对（实测 40/40），但 psutil **不在本项目 requirements 里**
   （环境里的 5.9.0 属于巧合），不为一条测试引入未声明依赖。

轮询循环也从 `try/except OSError` 改成 `if not pid_alive(pid): break`，
失败信息带上 pid 便于排查。

---

## 4. 验证

### 4.1 L1：判据核验（`tools/turn_job_verify/pid_alive_check.py`）7/7

| 断言 | 结果 |
|---|---|
| 活进程：tasklist 说活 | PASS |
| 活进程：os.kill 说活 | PASS |
| taskkill 后 tasklist 说死（权威判据） | PASS |
| **taskkill 后 os.kill 假阳** | PASS（这条 PASS 表示缺陷存在） |
| 不存在 pid：tasklist 说死 | PASS |
| 不存在 pid：os.kill 说死 | PASS |
| 50 轮 taskkill 后 tasklist 判活数 | PASS（0/50） |

**「os.kill 假阳」那条是刻意写成「期望成立」的** —— 它若某天变红，说明平台行为变了，
`pid_alive` 的实现该重新审视。它同时是「为什么不用 os.kill」的可复跑证据。

### 4.2 反向确认（关键）

只证明「修完全绿了」不够 —— 这个 bug 是概率性的，绿可能只是碰巧没触发。

做法：把测试判据临时换回 `os.kill`，**并额外注入一个故意持有已死子进程句柄的测试**
（人为造出假阳条件），跑全量：

```
反向确认（旧判据 + 故意持句柄）rc = 1
FAILED tests/test_process_registry.py::test_code_run_stop_kills_child - Faile...
1 failed, 1126 passed
→ 变红了，说明修复确实在起作用
```

修复版同样条件下全绿。

### 4.3 全量

修复后连跑 **3 次 1126 全绿**（178s / 189s / 188s）。

---

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `backend/tests/test_process_registry.py` | 新增 `pid_alive()` helper（含详尽注释说明为何不能用 `os.kill`）；轮询循环改用它；失败信息带 pid |
| `tools/turn_job_verify/pid_alive_check.py` | **新增**：判据核验 7 项，其中「os.kill 假阳」为期望成立的反面证据 |

---

## 6. 顺带记一条方法论

**「探针构造」本身会伪造现象**，这一轮踩了两次：

1. 阶段 4 的 `res.read(256)` —— `BufferedReader.read(n)` 会循环补齐到 n 字节，
   把逐帧到达的心跳伪造成「憋了 40 秒一起到」；
2. 这次的裸脚本一直持着 `Popen` 引用 —— 自己制造了 `os.kill` 假阳条件，
   却当成「Windows 平台普遍行为」。

两次都是**探针把被测对象的性质搞错了**，而代码本身没问题。这类错误比代码 bug 更难
发现，因为它伪装成「实测证据」。

对策：涉及「系统/平台行为」的结论，必须用**两个独立判据交叉验证**
（这次是 tasklist + psutil），且探针本身要能被反向确认打红。

---

## 7. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-11-15 | 修复 `test_code_run_stop_kills_child` 偶发失败。根因是判据 `os.kill(pid, 0)` 在 Windows 上对已死进程假阳（全量失败现场 50/50 轮 os.kill 与 tasklist/psutil 不一致）。改用 tasklist（Windows）/ `os.kill`（类 Unix）的 `pid_alive()`。判据核验 7/7 + 反向确认变红 + 全量 3 次 1126 全绿。纠正阶段 4 记录 §4.4 中「负载敏感」的定性 |

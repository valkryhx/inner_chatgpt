# ACP Client 驱动 GenericAgent —— 实施记录（2026-11-09）

> 一句话摘要：ACP 客户端在本机真 GA 上先做了 **manager 级 E2E**（不开浏览器、不启后端），
> 覆盖设计文档的三个验收场景：**预检与工具注入、哨兵写文件、多步（读 → 改 → 跑）、
> 长任务与停止**。全部在**全新进程、不预热任何缓存**的条件下通过；停止之后立刻再派新任务
> 也能跑通。期间发现的阻塞缺陷（预检探针被 bridge 的 stdout 重定向击穿）**已修复并复验**
> （commit `78c0b4f`）。随后又在主工作树补跑 **Web 真实 E2E**（真浏览器登录 + 真后端 +
> 真 GA + DB 持久化 + 卡片渲染 + UI 停止），结果见 §9。

> 日期：2026-11-09 · 状态：manager 级 E2E + **Web 真实 E2E** 均通过；§9 的 3 个观察项已于同日仅在内层修复（见 §10）· 设计文档：
> `docs/20261109-ACP-client驱动GenericAgent设计与实施.md`

---

## 1. 环境与事实锚定

| 项 | 值 |
|---|---|
| 工作树 | `D:/git_codes/inner_chatgpt/.worktrees/acp-client-ga`（分支 `feature/acp-client-ga`） |
| E2E 运行点 | `b106234`（其父提交即修复 `78c0b4f fix(acp): resolve the bridge path without importing it`）；本轮 `calc.py` 多步 E2E 与停止后继续验证在该点跑完 |
| 宿主 | Windows 11 26200，Git Bash / PowerShell 7 |
| 解释器 | `C:/anaconda3/python.exe`（3.12.4，Anaconda） |
| GA bridge | `C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py` |
| bridge SHA256 | `4e95c64bc64c1d0884ea9b70dcc8c25be9a4cb401c1893cdbc5d03be2491d6a5` |
| `agentInfo.version` | `0.1.0`（`agentInfo.name` = `genericagent-acp`，`title` = `GenericAgent`） |
| GA 模型配置 | `%APPDATA%/GenericAgent/llm.yaml`（18 个 profile，active=#16；**内容未读取、未落盘**） |
| 配置 | `INNER_ACP_GA_PYTHON=C:/anaconda3/python.exe`、`INNER_ACP_MODE=full_access`（只有这两个开关） |
| 验收级别 | **manager / 工具层**（§2–§6）+ **Web 真实 E2E**（§9：真浏览器登录、真后端、真 GA、SSE、DB 与卡片） |

§2–§6 先做 manager 级的原因：最初把整条 ACP 栈（transport → client → session → manager →
`ga_*` 工具层）按真实注入顺序在进程内跑通，事件进自建的最小 recorder（只有 `put()`，与
`EventBuffer` 同签名），便于逐层隔离；浏览器 + 登录流后来在主工作树补跑并通过（§9）。

---

## 2. 复现方式

```bash
cd /d/git_codes/inner_chatgpt/.worktrees/acp-client-ga

# 取 bridge 路径：⚠️ 这条命令的路径**经 stderr 输出**（bridge import 时把 stdout 重定向到
# stderr，与 §6.1 同因），所以下面用 2>&1 把它显示出来。
C:/anaconda3/python.exe -c "import frontends.acp.genericagent_acp_bridge as b; print(b.__file__)" 2>&1

# 等价且无副作用的写法（只解析路径、不 import 目标模块，就是 78c0b4f 的修法）
C:/anaconda3/python.exe -c "import importlib.util as u; print(u.find_spec('frontends.acp.genericagent_acp_bridge').origin)"

# 摘要
python -c "import hashlib;print(hashlib.sha256(open(r'C:/anaconda3/Lib/site-packages/frontends/acp/genericagent_acp_bridge.py','rb').read()).hexdigest())"
```

验证脚本是**一次性 throwaway**（放在 gitignored 的 `work/`，跑完即删，**未入库**）。
关键点是**不复用任何预检缓存**：全新进程里走 `subagent/session.py` 的注入闸
（`preflight()` → `build_acp_tools()`），再通过 **5 个 `ga_*` 工具**（模型实际看到的那一层）派活。骨架：

```python
import asyncio, sys
sys.path.insert(0, "backend")
from app.acp.config import load_acp_settings
from app.acp.manager import AcpSessionManager
from app.acp.tools import build_acp_tools      # 真实注入闸的第二半
from app.subagent.context import AgentContext

class Recorder:                                 # 只用到 put()
    def __init__(self): self.events = []
    def put(self, ev): self.events.append(ev)

ctx = AgentContext(user_id="e2e6", session_id="s6", message_id="m6", generation_id="g6")
settings = load_acp_settings({"INNER_ACP_GA_PYTHON": "C:/anaconda3/python.exe",
                              "INNER_ACP_MODE": "full_access"})
manager = AcpSessionManager(ctx=ctx, buffer=Recorder(), settings=settings)
bridge = await asyncio.to_thread(manager.preflight)   # ← 闸：现在这里直接成功
tools = {t.name: t for t in build_acp_tools(manager)}

spawn = await tools["ga_spawn"].call({"task": "...", "timeout_s": 900})
await tools["ga_wait"].call({"agent_id": spawn["agent_id"], "timeout_s": 900})
print(await tools["ga_result"].call({"agent_id": spawn["agent_id"], "full": True}))
await tools["ga_stop"].call({"agent_id": aid})        # 或 manager.stop(aid)
```

停止路径的分层探针同上：每步之后轮询
`manager._sessions[aid].process_handle.returncode`，并用 `Get-CimInstance Win32_Process`
按命令行核对真实进程（见 §5.5）。

本次运行的原始 JSON 证据保留在本地 `work/*.json`（同样未入库）。

---

## 3. 场景一：哨兵任务（真 GA 写文件）—— ✅ 通过

### 3.1 注入闸（先决条件）

全新进程、未预热缓存、`INNER_ACP_GA_PYTHON=C:/anaconda3/python.exe`：

| 观测点 | 实际值 |
|---|---|
| `preflight()` | **成功**，0.047 s，返回 `C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py` |
| 该文件 SHA256 | `4e95c64bc64c1d0884ea9b70dcc8c25be9a4cb401c1893cdbc5d03be2491d6a5` |
| `build_acp_tools()` 注入 | **5 个**：`ga_spawn` / `ga_wait` / `ga_result` / `ga_send` / `ga_stop` |

### 3.2 任务与结果

- 哨兵：`ACP_GA_OK_9c7d883e`（当次运行随机生成，8 位 hex）
- 任务书（原样）：

```text
请使用 file_write 工具在当前工作目录创建文件 acp_ga_e2e.txt，文件内容严格为这一行：ACP_GA_OK_9c7d883e。不要询问用户，不要写别的内容。完成后只回复 DONE。
```

| 观测点 | 实际值 |
|---|---|
| `ga_spawn` | `{"agent_id": "ga_212bbaa6", "status": "running"}` |
| `ga_wait` | `timed_out: false`，`status: completed`，`last_seq: 11` |
| `ga_result` | `status: completed`、`prompts: 1`、`tool_calls: 1`、`stop_reason: end_turn`、`error: ""` |
| `agentInfo` | `{"name": "genericagent-acp", "title": "GenericAgent", "version": "0.1.0"}` |
| 工具步骤 | 1 步：`tool_start` `file_write`，rawInput `{"path": "acp_ga_e2e.txt", "content": "ACP_GA_OK_9c7d883e\n"}` → `tool_result` `{"status": "success", "writed_bytes": 19}` |
| 产物文件 | `backend/data/workspaces/e2e5/acp_ga_e2e.txt`，**19 字节**：`ACP_GA_OK_9c7d883e` + 结尾 `\n` |
| 哨兵是否匹配 | **匹配**（文件内容含该哨兵；模型这次多带了一个行尾换行，修复前那一轮是 18 字节、无换行） |
| 端到端耗时 | 7.9 s（含 GA 启动 + 两次 LLM 调用） |
| `EventBuffer` 事件 | 21 条，类型只有 `subagent_start` / `subagent_event` / `subagent_end`（与 P2 子代理同一套） |

> 关于字节差异：任务书要求「内容严格为这一行」，GA 两轮分别写成 18 B（无换行）与 19 B（带 `\n`）。
> 这是 **GA 侧模型行为**（`file_write` 原样收到它给的 content），不是客户端问题；
> 验收判据是「文件存在且含哨兵」，两轮都满足。

---

## 4. 场景 2b：多步任务（读 → 改 → 跑）—— ✅ 通过

### 4.1 预置的有 bug 脚本

`backend/data/workspaces/e2e6/calc.py`（283 B，运行前由脚本写入）：

```python
"""小型计算脚本：add 有一点小问题。"""


def add(a, b):
    return a - b


def main():
    print("test: add(2, 3) =", add(2, 3))
    assert add(2, 3) == 5, f"add(2, 3) 应为 5，实际 {add(2, 3)}"
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
```

### 4.2 任务书与结果

```text
当前工作目录里有一个 calc.py，里面的 add 函数是错的（add(2, 3) 的结果不对）。请：① 用 file_read 读取 calc.py；② 用 file_patch 或 file_write 把 add 函数改对；③ 用 code_run 运行 `python calc.py` 验证它输出 ALL TESTS PASSED；④ 只回复一行：运行结果 + 退出码。不要询问用户。
```

| 观测点 | 实际值 |
|---|---|
| `ga_spawn` / `ga_wait` | `ga_d90f6ffd`，`timed_out: false`，`status: completed` |
| `ga_result` | `status: completed`、`tool_calls: 3`、`stop_reason: end_turn` |
| **工具步骤数** | **3 步，且顺序正好是 读 → 改 → 跑**：`file_read` → `file_patch` → `code_run`（`EventBuffer` 里数 `tool_start`） |
| 读 | `file_read {"path": "calc.py"}` |
| 改 | `file_patch {"path": "calc.py", "old_content": "    return a - b", "new_content": "    return a + b"}` → `{'status': 'success', 'msg': '文件局部修改成功'}` |
| 跑 | `code_run` 内 `subprocess.run(['python','calc.py'])` → `[Status] Exit Code: 0`、`test: add(2, 3) = 5`、`ALL TESTS PASSED` |
| GA 的答复 | `运行结果：test: add(2, 3) = 5 / ALL TESTS PASSED，退出码 0。` |
| `calc.py` 是否被改 | **是**：`return a - b` 变成 `return a + b`（文件长度仍 283 B，内容哈希变了） |
| 独立复核（我方另起进程跑） | `python calc.py` → **rc=0**，stdout `test: add(2, 3) = 5` / `ALL TESTS PASSED` |
| 端到端耗时 | 15.8 s |

> 这一节对应任务书 Step 2b 的验收：`ga_result` 提到运行成功 ✅、卡片里至少 3 个工具步骤（读 / 改 / 执行）✅、
> `calc.py` 内容确实被修改 ✅。判据里的「卡片」在 manager 层等价于 `EventBuffer` 的
> `subagent_event` 流（见 §7 的 DB/UI 缺口）。

---

## 5. 场景三：长任务与停止 —— ✅ 分层通过

- 任务书：要求 GA 用 `code_run` 跑 `python -c "import time; time.sleep(60)"`，跑完回 DONE。
- 时序：`ga_spawn` → 等 5 s → `ga_stop`（= `manager.stop`）→ 观察进程句柄 `returncode`
  → 再**按产品 `/stop` 顺序手工复现**（`acp_manager.stop_generation` → `process_registry.stop_generation`）。

### 5.1 单层 `stop()`：只取消 prompt，不杀进程（设计如此）

| 观测点 | 实际值 |
|---|---|
| `ga_stop` | `{"stopped": ["ga_37016b0e"]}` |
| 会话状态 | `stopped`（不是 `errored`） |
| 句柄 `returncode`（stop 后 10 s，0.5 s 轮询 20 次） | **始终 `None`** |
| CIM（按命令行） | `25868,34100`——两条 bridge 进程都还在 |
| `tasklist \| findstr genericagent_acp_bridge` | `<空>`（假阴性，见 §5.5） |

这是**设计**：设计文档 §9.4 写明「优雅路径：对每个会话发 `session/cancel`，等 prompt
响应最多 2s；**返回则正常收尾，不杀进程**」。真 GA 对 `session/cancel` 响应极快
（bridge stderr：`Abort current task...` / `User aborted the task.`），2 s 宽限内 prompt
就收尾了，**没走到 `kill_tree_now` 分支**；bridge 进程留着等下一次 prompt（`ga_send` 复用）。

### 5.2 按产品 `/stop` 顺序手工复现：进程被回收

| 观测点 | 实际值 |
|---|---|
| `process_registry.stop_generation()` | 杀掉 **2** 个租约（当时两个 GA 会话都还开着），0.33 s |
| `returncode` | **`1`**（首次轮询即非 None） |
| 10 s 内回收 | **是** |
| CIM（按命令行） | `<空>`：无残留 bridge 进程 |
| 会话状态 | `stopped` |

> 说明：真机上 `kill_tree_now`（2 s 未响应才触发）从未命中——GA 总是先优雅收尾。
> 本记录验证的是**它之后的** `process_registry` 硬杀，产品 `/stop` 正是这么串的。

### 5.3 收尾路径 `close_all()`（本轮结束）

| 观测点 | 实际值 |
|---|---|
| `close_all()` | `ok` |
| CIM 复核 | `<空>`（无残留） |

### 5.4 停止之后能否立刻继续（manager 层证据）

「主对话可以立即发送」的 chat 层判据需要完整 turn runtime + UI（见 §7 缺口）。
manager 层可验证的部分是「停止后同一个 manager 立刻能再派活」，实测：

| 步 | 实际值 |
|---|---|
| ① 起长任务并 `ga_stop` | `{"stopped": ["ga_250041bb"]}`，状态 `stopped` |
| ② **立刻**（同一 manager、同一 generation）再 `ga_spawn` 一个轻任务 | `{"agent_id": "ga_970b5ab2", "status": "running"}`——没有被配额或会话名额挡住 |
| ③ `ga_wait` | `timed_out: false`，`status: completed`，`stop_reason: end_turn` |
| ④ 结果 | GA 复述了本次随机 marker `MULTISTEP_3abf7f1a`（**标记命中**），耗时 2.7 s |
| ⑤ `close_all()` | `ok` |

### 5.5 `tasklist | findstr genericagent_acp_bridge` 复核（**假阴性**）

先给结论：**这条命令对 GA 恒为空，不能用它判断 GA 是否停掉。**

原因：`tasklist` 只列**镜像名**（`python.exe`），输出里根本没有命令行；GA bridge 是
「用 python 跑一个 .py」，镜像名永远是 `python.exe`。三次实测：

| 时刻 | `tasklist \| findstr /i genericagent_acp_bridge` | 真实情况（CIM 按命令行） |
|---|---|---|
| GA 正在跑（pid 25868 / 34100，`returncode` 仍 None） | `<空>` | `25868,34100` |
| `ga_stop` 之后（10 s 轮询期间） | `<空>` | `25868,34100`（仍在，符合 §5.1） |
| 硬杀兜底 / `close_all()` 之后 | `<空>` | `<空>` |

（Git Bash 里直接写 `findstr /i ...` 会被 MSYS 把 `/i` 当路径、报 `FINDSTR: 无法打开 i`；
上表结果都经 `cmd /c` 执行，才是 `findstr` 的真实输出。）

能区分「在跑 / 停了」的等价复核是**按命令行过滤**：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*genericagent_acp_bridge*' } |
  ForEach-Object { $_.ProcessId }
```

另一条（同样有效）：`Get-Process -Id <bridge pid>` 直接看 pid 是否还在。
本次全部运行结束后复核：无残留 bridge 进程。

---

## 6. E2E 期间发现并修复的缺陷（预检探针被 stdout 重定向击穿）

### 6.1 缺陷（复跑前的形态）

按设计文档 §11.3 写的探针 `-c "import <module> as b; print(b.__file__)"` 在**真机恒失败**：

```text
$ <python> -c "import frontends.acp.genericagent_acp_bridge as b; print(b.__file__)"
rc = 0
STDOUT = ''
STDERR = 'C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py\n'
→ AcpUnavailableError: GenericAgent ACP bridge 不可用: <同一路径>
```

根因：`genericagent_acp_bridge.py:11-45` 在模块 import 期就把 stdout 抢走——Windows 下
`os.dup2(sys.stderr.fileno(), sys.__stdout__.fileno())`（**fd 1 本身被重定向到 stderr**），
再把 `sys.stdout` 换成 `_StdoutToStderrRouter`。这是 bridge 的**正确设计**（JSON-RPC 要独占
stdout），但让「靠 print 取路径」的探针失效：print 进了 stderr，而 `preflight()` 只读 stdout
且要求非空，于是**健康安装被判成不可用**。

影响面（修复前）：`spawn()` 第一行就 `self.preflight()`，所以真实部署里 `ga_spawn` 永远
拿不到会话；按设计 §11.3，预检失败时整个 `ga_*` 工具集不注册 → **ACP 能力在真机不可用**。
单测用假模块（不重定向 stdout），所以一直是绿的——设计文档 §3.3 已记录「bridge 会重定向
stdout」，§11.3 的探针却仍用 `print`，属实现与已知事实冲突。

### 6.2 修复（commit `78c0b4f`，非本任务提交）

`78c0b4f fix(acp): resolve the bridge path without importing it`：

- 探针改为 **`importlib.util.find_spec(<module>).origin`** ——只解析模块路径、**不执行目标
  模块**，因此不会再触发 bridge 的 stdout 重定向；同时保留「没装 / 装错 Python / 父包缺失」
  在 spawn 之前就判出来的能力（`find_spec` 找不到时返回 `None`，实现里补了明确的错误文案）。
- 同步更新 `backend/app/acp/manager.py` 的 docstring 与注释，说明为什么**不能** import。
- 新增回归测试（`backend/tests/test_acp_manager.py`，+39 行），覆盖「bridge import 会重定向
  stdout」这一副作用——原有假模块测试抓不到。

### 6.3 复验（本节数据来自修复后的全新进程）

| 观测点 | 结果 |
|---|---|
| `preflight()` | 成功，0.047 s |
| 返回路径 | `C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py` |
| SHA256 | `4e95c64bc64c1d0884ea9b70dcc8c25be9a4cb401c1893cdbc5d03be2491d6a5` |
| 工具注入 | 5 个 `ga_*` 全部注入，`ga_spawn` 正常开会话 |

即：**预检成功形态已在本机真 GA 上验证过**，不再依赖任何缓存预热；§2 里第一条 `-c` 命令
只作「解释器里装了 bridge」的旁证，路径从 stderr 读（与本节同因），**不是**预检的实现方式。

---

## 7. 未覆盖项与已知限制

| 项 | 状态 | 说明 |
|---|---|---|
| Web UI E2E | ✅ **已跑** | 2026-11-09 在主工作树用真浏览器登录 `127.0.0.1:8000` 跑通；见 §9 |
| DB `blocks` / 卡片渲染 | ✅ **已验证** | §9.2 实测 `type=subagent`、`agent_id=ga_67769141#1`、`role=ga`、`status=completed`、31 条 events；§9.4 实测「GA 外援」卡片、实时事件与停止态 |
| 「主对话可以立即继续发送」 | ✅ **已验证** | manager 层 §5.4 + chat/SSE 层 §9.2/§9.3：停止或完成后同会话立即再发，均返回 `done complete` |
| GA 内部 subagent 事件 | **未透出** | GA 自己的子代理（workflow / child_agent）事件不会变成 `subagent_*`；ACP 只把 `session/update` 的 `agent_message_chunk` / `tool_call*` 映射出来，外层只能看到 GA 一层 |
| 跨轮续跑 | **不支持** | 会话按 generation 生命周期管理，本轮结束 `close_all()`；bridge `agentCapabilities.loadSession=false`，无法跨轮恢复 GA 侧上下文 |
| `session/set_mode` | 未触发 | bridge 默认就是 `full_access`，与本次配置相同，该分支未被真实执行 |
| 硬杀兜底（2 s 未响应） | 未在真 GA 上触发 | 真 GA 对 `session/cancel` 响应 < 0.2 s，`kill_tree_now` 分支由单测覆盖，真机未命中；本记录验证的是**排在它之后**的 `process_registry` 硬杀 |
| GA 模型 / 计费 | 已发生 | GA 用自己的 `llm.yaml`（18 profiles），每轮会话至少 2 次 LLM 调用，属设计 §11.2 已接受的「两套计费」 |
| `INNER_ACP_*` 其余开关 | 未逐一验证 | 只验证了 `INNER_ACP_GA_PYTHON` + `INNER_ACP_MODE=full_access` 这一组 |

---

## 8. 结论

1. **ACP 栈对真 GA 是通的**：预检（`find_spec`）→ 工具注入（5 个）→ 握手 → `session/new`
   → `session/prompt` → `session/update` 流式 → 工具调用真落盘/真执行 → `stopReason: end_turn`。
2. **三个验收场景全部通过**：哨兵（7.9 s）、多步读→改→跑（3 步，15.8 s，`ALL TESTS PASSED`）、
   长任务 + 停止（分层回收，且停止后 2.7 s 内可继续派活）。
3. **停止是分层的**：优雅层（`ga_stop`/`stop`）只取消 prompt、不杀进程（设计如此）；
   真正回收靠收尾 `close_all()` 或产品 `/stop` 顺序里的 `process_registry` 硬杀（`returncode=1`）。
4. **主要缺陷已闭环**：预检探针在真机失效的问题由 `78c0b4f` 用
   `importlib.util.find_spec(...).origin` 修掉，并补了带 stdout 重定向副作用的回归测试。
5. **Web 层已补齐**（§9）：真浏览器登录、DB blocks / 卡片渲染、chat/SSE 停止与「停止后立即发送」均通过；
   剩余边界是 GA 内部 subagent 事件、跨轮续跑、硬杀兜底（见 §7）。

---

## 9. Web 真实 E2E（2026-11-09 补充，已执行）

> 本节补齐 §7 原来的三项未覆盖：**Web 登录流、DB `blocks` / 卡片渲染、chat/SSE 层停止后立即发送**。
> 环境是仓库主工作树 `D:/git_codes/inner_chatgpt`（非 worktree），后端单端口托管重建后的 `frontend/dist`。

### 9.1 环境与账号

- 后端：`C:/anaconda3/python.exe run.py`，`127.0.0.1:8000`；启动环境
  `INNER_ACP_GA_PYTHON=C:/anaconda3/python.exe`；健康检查返回 `{"ok":true,...}`。
- 前端：`npm run build` 重建（包含 `SubagentCard` 的 `ga → GA 外援` 标签）。
- 账号：`wfd3970794@fix.local`（仓库测试脚本约定的一次性测试账号；密码不落文档）。
- 模型：该用户自己的 `user_llm_configs` 配置 `moma/deepseek-latest-flash`
  （仓库根没有 `llm.yaml`，因此这也验证了「用户级模型配置」路径）。
- 浏览器：`playwright-cli` 实际打开页面、填表登录，不是只发 HTTP。

### 9.2 场景 A：浏览器内派 GA 写哨兵文件 —— ✅ 通过

| 观测点 | 证据 |
|---|---|
| 会话 / 随机 token | `co_6ec2a648100f` / `WEBE2EA1AE2DB773` |
| 工具调用 | 3.2 s `ga_spawn`；17.4 s `ga_result`；两者 `is_error=false` |
| 子代理卡片 id | `ga_67769141#1`（会话级 id + `#1`） |
| 事件量 | `subagent_start` 1、`subagent_event` 31、`subagent_end` 1、`text_delta` 54 |
| 完成时间 | `ga_wait` 15.9 s 返回 `completed`；整轮 `done status=complete`，23.17 s |
| 文件落盘 | `backend/data/workspaces/u_2c13cd6b707e/acp_web_e2e_WEBE2EA1AE2DB773.txt`，字节精确为 `WEBE2EA1AE2DB773`（无换行） |
| DB `blocks` | assistant 消息含 `type=subagent`、`agent_id=ga_67769141#1`、`role=ga`、`status=completed`、31 条 events |
| GA 进程回收 | 轮末 `genericagent_acp_bridge` 进程数 **0** |
| 立即续发 | 同会话再发「只回复 OK」→ `done status=complete`，正文 `OK` |
| 浏览器卡片 | `GenericAgent · 创建 e2e 文本文件  GA 外援 · 已完成 · 2 个工具`，展开可见 `code_run` 时间线；截图 `work/acp_web_e2e/ga_card_visible.png`、`work/acp_web_e2e/ga_card_completed.png` |

> `subagent_end` 的 `result` 为空是**设计行为**：有文本事件时卡片时间线已经展示过，
> `session.py::_finish_prompt` 不再重复塞 `result`；GA 的最终答复由 `ga_result` 返回。

### 9.3 场景 B：chat/SSE 层停止 + 停止后立即发送 —— ✅ 通过（终帧有边界）

| 观测点 | 证据 |
|---|---|
| 会话 / token | `co_5e1b6df2ac3f` / `STOPBBA4C957EC` |
| GA 任务 | 先 `time.sleep(90)`，再写文件；调用后进入 `ga_wait` |
| `/stop` 响应 | HTTP 200，`{"ok":true,"stopped":1,"subagents_stopped":0,...}`，0.53 s |
| 进程回收 | 停止后 `genericagent_acp_bridge` 进程数 **0** |
| DB 状态 | assistant 消息 `status=stopped`；subagent 块 `status=stopped` |
| 立即续发 | 同会话立刻再发「只回复 OK」→ 200，`done complete`，正文 `OK`；无需刷新或重试 |

**边界**：停止时服务端直接关闭 SSE，客户端看到
`RemoteProtocolError: peer closed connection without sending complete message body`，
没有 `done` / `subagent_end` 终帧。UI 停止按钮、DB 状态与后续发送都正确；
若第三方客户端依赖终帧，需要把「连接被关闭」也当作停止处理。该行为来自现有 chat 停止路径，
不是 ACP 新增。

### 9.4 场景 C：浏览器内实时卡片 + UI 停止 —— ✅ 通过，三个观察项（均已修复）

- 通过 UI 新建会话并发送长任务；约 10 s 时快照显示
  `GenericAgent · 等待120秒后回复DONE  GA 外援 · 1 个工具`、`ga_wait ... 运行中`，
  底部有 `Working 23s` 与「停止」按钮；截图 `work/acp_web_e2e/ga_card_live_running.png`。
- 点击「停止」后约 4 s，卡片变为 `GA 外援 · 已停止 · 1 个工具`；
  `genericagent_acp_bridge` 进程数 **0**；截图 `work/acp_web_e2e/ga_card_stopped.png`。
- 刷新页面后卡片仍显示 `已停止`（会话 `co_febd66956e21`）。
- **观察项 1（Minor，既有 UI 渲染；✅ 已修复，见 §10.1）**：被中断的工具步骤 `ga_wait` 在刷新后仍显示「运行中」。
  根因是 `MessageList.tsx::toolState()` 用 `result === undefined` 判运行中，未按消息
  `stopped` 兜底；子代理卡片本身有兜底（`MessageList.tsx:248-253` 会把非流式消息里的
  `running` 子代理渲染为 `stopped`），所以卡片状态正确，只有工具步骤文案不一致。
- **观察项 2（Minor，数据层竞态；✅ 已修复，见 §10.2）**：硬取消时最后一次 `_persist` 可能被一起取消，
  DB 子代理块可停留在 `running`（本次 UI 停止后的 DB 即为 `running`；§9.3 的 API 停止那次为
  `stopped`）。前端已用上述兜底渲染成「已停止」，用户可见状态正确；直接读 DB 的下游需要
  应用同一规则。属停止路径与持久化竞态，不是 ACP 断连。
- **观察项 3（噪声，不阻塞；✅ 已修复，见 §10.3）**：后端 stderr 在停止/客户端断开时出现若干 `CancelledError`
  回溯（`team_store.release_generation` 被取消）。下一次发送的陈旧租约自愈逻辑正常生效，
  未出现 409 或卡死；建议后续用取消安全的持久化收口。

### 9.5 Web 层结论

1. **全链路通过**：真浏览器 + 真登录 + 真后端 + 真 GA：`ga_spawn` → 实时卡片 → 文件落盘
   → DB `blocks` → 停止 → 立即续发。
2. 「GA 外援」角色标签、实时事件、完成/停止状态在 UI 正确显示；刷新后状态仍可读。
3. 三个观察项都不影响主流程，且已仅在内层修复（§10）：中断步骤显示「已中断」、停止后 DB 块持久化为 `stopped`、取消不再打印回溯；未改 GenericAgent。
4. §7 原来列出的「Web UI E2E」「DB blocks / 卡片渲染」「停止后立即发送」三项**已补齐**。


---

## 10. 停止路径小问题修复（2026-11-09，仅改 inner_chatgpt）

> 用户要求：**不动 GenericAgent 一行代码**。本节所有改动都在 inner_chatgpt；
> 本轮没有对 GenericAgent 仓库执行任何写操作（GA 工作树里若有既有改动，与本轮无关）。

### 10.1 中断的工具步骤显示「运行中」→「已中断」

- 新增 `frontend/src/toolState.ts`：未拿到 result 的步骤，只有消息仍在流式时才是 `running`；
  消息已结束则返回 `stopped`。
- `MessageList.tsx` 的 `StepRow` / `StepGroup` / `renderBlocks` 传入 `interrupted = !isActiveStream`；
  步骤与分组文案显示「已中断」，CSS 新增 `.status-dot.stopped` / `.step-group-sum.stopped`。
- 测试：`frontend/tests/toolState.test.ts`（4 条，`npm test`）。
  反向确认：移除 `toolState.ts` 后测试全红。

### 10.2 DB 子代理块停止后仍为 `running`

- 根因：流被取消后，`except asyncio.CancelledError` 里的 `await _persist(...)` 会立刻再次抛
  `CancelledError`，终态写库被丢掉；`finally` 里的租约释放同样会被取消。
- 修复（`backend/app/routers/chat.py`）：新增 `_spawn_background` / `_drain_background_tasks`；
  取消路径用 `_finalize_and_persist_after_cancel(...)` 把终态写库放到新任务，
  `was_unknown` 收尾也走同一后台写。
- 测试：`backend/tests/test_stop_persistence.py::test_cancel_terminal_finalizes_and_persists_in_background`。
  反向确认：stash `chat.py` 后测试变红。

### 10.3 停止时 `CancelledError` 回溯噪声

- 租约释放改为 `_release_or_background(...)`：正常时 await；被取消时把释放动作交给新任务，
  不再把 `team_store.release_generation` 的取消回溯打到 stderr。
- `_cleanup_turn` 对 `host.close_all` / `acp.close_all` 的 `CancelledError` 单独处理：
  记录 info 级「cancelled; settling in background」，仍然调度 `_settle_turn`，不再 `exc_info=True`。
- 测试：`test_release_generation_after_cancel_is_retried_in_background`、
  `test_cleanup_acp_cancellation_logs_without_traceback`（均含反向确认）。

### 10.4 验证结果

- 后端全量：**246 passed**（原 243 + 3 条停止持久化/噪声测试）。
- 前端：`npm test` 4 passed；`npm run build` 成功。
- 真机 Web 复验（固定后端 + 重建前端）：
  - UI 停止后立即显示 `ga_wait ... 已中断`，刷新后仍为「已中断」；
  - DB assistant 消息 `status=stopped`，子代理块 `status=stopped`（会话 `co_0dced37cf96f`）；
  - 停止后同会话立即再发返回 `done complete`；
  - `backend3.err.log` 8 行，无 `Traceback` / `CancelledError` / `release generation lease failed`；
  - `genericagent_acp_bridge` 进程数 0。
- **未改 GenericAgent**：桥、GA 配置、GA 代码均未动。
- 仍未改的边界：停止时 SSE 直接关闭、没有 `done` 终帧（§9.3），这次不在修复范围。

---

## 11. 边界分析：停止时 SSE 直接关闭，会不会影响 GA？

结论：**不影响 GA 的进程生命周期与会话正确性，可以保留。** 影响只在客户端协议层：
第三方 SSE 客户端拿不到 `done` 终帧，需要把「连接被关闭」当作停止处理。

### 11.1 两条通道是解耦的

- SSE 是 **server → 浏览器** 的单向流；GA 是 **server → 子进程** 的 ACP stdio 通道。
- `/stop` 的调用顺序是先停 GA、后杀 SSE（`request_stop` → `stop_acp_generation`
  ≤2 s 优雅窗口 → `cancel_generation` → 子代理 → `code_run`），所以 SSE 关闭时
  GA 的停止动作已经发出。
- 即使 SSE 任务被取消，`_event_stream` 的 `finally` 仍会走 `_cleanup_turn`：
  同步执行 `acp.stop_all_now()`（`mark_stopped` + `kill_tree_now`），再异步 `close_all()`。
  取消安全的后台任务保证租约释放与终态写库（§10）。

### 11.2 实测一：用户点停止

- 会话 `co_0dced37cf96f`：`/stop` 返回 `stopped=1`，0.59 s；GA bridge 进程数 **0**；
  DB 消息与子代理块均为 `stopped`；同会话立刻再发成功；`backend3.err.log` 无回溯。

### 11.3 实测二：客户端直接断开、不调用 `/stop`

这是比「停止时关 SSE」更严格的场景：

- 会话 `co_1b9e10aa04a5`，GA 任务进入 `sleep 60` 后，客户端在 `subagent_start` 后
  2.95 s **直接关闭 SSE 连接**，没有调用 `/stop`。
- 关闭后 **1 s** 复查：GA bridge 进程数 **0**；assistant 消息 `status=stopped`；
  子代理块 `status=stopped`。
- 同会话立刻再发「只回复 OK」→ HTTP 200、`done complete`、正文 `OK`；
  最终 bridge 进程数仍为 **0**。

即：即使只关 SSE、不发停止请求，服务端的请求取消也会触发 `_cleanup_turn`，
把 GA 子进程收掉；不存在「SSE 断了、GA 还在后台跑」的泄漏。

### 11.4 对 GA 的实际影响边界

- **没有进程泄漏**：两种路径实测 bridge 进程数都归零。
- **没有跨轮状态损坏**：GA 会话按 generation 生命周期管理，本轮结束即回收；
  bridge `loadSession=false`，本来就不保留跨轮上下文。
- **在途工作会被丢弃**：这是「停止」的语义，不是 SSE 关闭造成的额外影响；
  若 GA 正在写文件，可能留下写到一半的文件，和任何中断一样。
- **唯一损失在客户端**：没有 `done` / `subagent_end` 终帧。当前 UI 用本地停止状态
  和刷新兜底处理；第三方客户端需要把连接关闭当作终态。

因此本次保留「停止时 SSE 直接关闭」的行为，不改协议；如果未来要服务第三方
SSE 客户端，可以单独加「先发 `done` 再关流」或心跳，与 GA 侧无关。

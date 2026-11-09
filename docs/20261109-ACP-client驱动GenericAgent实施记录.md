# ACP Client 驱动 GenericAgent —— 实施记录（2026-11-09）

> 一句话摘要：ACP 客户端在本机真 GA 上做了 **manager 级 E2E**（不开浏览器、不启后端），
> 覆盖设计文档的三个验收场景：**预检与工具注入、哨兵写文件、多步（读 → 改 → 跑）、
> 长任务与停止**。全部在**全新进程、不预热任何缓存**的条件下通过；停止之后立刻再派新任务
> 也能跑通。期间发现的阻塞缺陷（预检探针被 bridge 的 stdout 重定向击穿）**已修复并复验**
> （commit `78c0b4f`）。

> 日期：2026-11-09 · 状态：manager 级 E2E 全通过（含 1 个已修缺陷）· 设计文档：
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
| 验收级别 | **manager / 工具层**（进程内构造 `AcpSessionManager` + `build_acp_tools`），不是 Web UI E2E |

为什么不是浏览器 E2E：本任务明确「浏览器 + 登录流超出本工作树范围」，因此把整条 ACP 栈
（transport → client → session → manager → `ga_*` 工具层）按真实注入顺序跑通，
事件进自建的最小 recorder（只有 `put()`，与 `EventBuffer` 同签名）。

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
| Web UI E2E | **未跑** | 浏览器 + 登录流超出本工作树范围；本记录只到 manager / 工具层 |
| DB `blocks` / 卡片渲染 | **未验证** | 任务书 Step 2 的 `blocks.type=="subagent"`、`agent_id` 含 `#1`、前端「GA 外援」卡片属于 turn runtime + 持久化 + UI 三段，本记录只在 manager 层等价验证了 `EventBuffer` 的 `subagent_start/_event/_end` 流。**DB blocks / card rendering not exercised at manager level (requires full turn runtime + UI).** |
| 「主对话可以立即继续发送」 | **部分验证** | manager 层已证：`ga_stop` 之后同一 manager 立刻再派任务，2.7 s 完成并复述随机 marker（§5.4）。**chat / SSE 层的这一段未验证**（需要完整 turn runtime + 前端）。 |
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
5. **剩余边界清楚**（见 §7）：Web UI E2E、DB blocks / 卡片渲染、chat 层「停止后立即发送」、
   GA 内部 subagent 事件、跨轮续跑均未覆盖。

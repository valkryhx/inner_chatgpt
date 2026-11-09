# ACP Client 驱动 GenericAgent —— 实施记录（2026-11-09）

> 一句话摘要：ACP 客户端在本机真 GA 上做了 **manager 级 E2E**（不开浏览器、不启后端）。
> **预检、哨兵任务、停止级联全部通过**：全新进程、**不预热任何缓存**，预检走
> `importlib.util.find_spec` 拿到 site-packages 路径，5 个 `ga_*` 工具正常注入；
> GA 用 `file_write` 写出哨兵并回 `DONE`；停止按产品真实顺序回收进程。
> 期间发现的阻塞缺陷（预检探针被 bridge 的 stdout 重定向击穿）**已修复并复验**（commit `78c0b4f`）。

> 日期：2026-11-09 · 状态：E2E 通过（含 1 个已修缺陷）· 设计文档：
> `docs/20261109-ACP-client驱动GenericAgent设计与实施.md`

---

## 1. 环境与事实锚定

| 项 | 值 |
|---|---|
| 工作树 | `D:/git_codes/inner_chatgpt/.worktrees/acp-client-ga`（分支 `feature/acp-client-ga`） |
| 复核时的 HEAD | `78c0b4f fix(acp): resolve the bridge path without importing it` |
| 宿主 | Windows 11 26200，Git Bash / PowerShell 7 |
| 解释器 | `C:/anaconda3/python.exe`（3.12.4，Anaconda） |
| GA bridge | `C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py` |
| bridge SHA256 | `4e95c64bc64c1d0884ea9b70dcc8c25be9a4cb401c1893cdbc5d03be2491d6a5` |
| `agentInfo.version` | `0.1.0`（`agentInfo.name` = `genericagent-acp`，`title` = `GenericAgent`） |
| GA 模型配置 | `%APPDATA%/GenericAgent/llm.yaml`（18 个 profile，active=#16；**内容未读取、未落盘**） |
| 配置 | `INNER_ACP_GA_PYTHON=C:/anaconda3/python.exe`、`INNER_ACP_MODE=full_access`（只有这两个开关） |
| 验收级别 | **manager 级**（进程内构造 `AcpSessionManager`），不是 Web UI E2E |

为什么不是浏览器 E2E：本任务明确「浏览器 + 登录流超出本工作树范围」，因此把整条 ACP 栈
（transport → client → session → manager → `ga_*` 工具层）按真实注入顺序跑通，
事件进自建的最小 recorder（只有 `put()`，与 `EventBuffer` 同签名）。

---

## 2. 复现方式

```bash
cd /d/git_codes/inner_chatgpt/.worktrees/acp-client-ga

# 预检事实（解释器 + bridge 路径 + 摘要）
C:/anaconda3/python.exe -c "import frontends.acp.genericagent_acp_bridge as b; print(b.__file__)"
python -c "import hashlib;print(hashlib.sha256(open(r'C:/anaconda3/Lib/site-packages/frontends/acp/genericagent_acp_bridge.py','rb').read()).hexdigest())"
```

验证脚本是**一次性 throwaway**（放在 gitignored 的 `work/`，跑完即删，**未入库**）。
关键点是**不复用任何预检缓存**：全新进程里直接走 `subagent/session.py` 的注入闸
（`preflight()` → `build_acp_tools()`），再通过 **5 个 `ga_*` 工具**（模型实际看到的那一层）
派活。骨架：

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

ctx = AgentContext(user_id="e2e5", session_id="s5", message_id="m5", generation_id="g5")
settings = load_acp_settings({"INNER_ACP_GA_PYTHON": "C:/anaconda3/python.exe",
                              "INNER_ACP_MODE": "full_access"})
manager = AcpSessionManager(ctx=ctx, buffer=Recorder(), settings=settings)
bridge = await asyncio.to_thread(manager.preflight)   # ← 闸：现在这里直接成功
tools = {t.name: t for t in build_acp_tools(manager)}

spawn = await tools["ga_spawn"].call({"task": "...", "timeout_s": 600})
await tools["ga_wait"].call({"agent_id": spawn["agent_id"], "timeout_s": 600})
print(await tools["ga_result"].call({"agent_id": spawn["agent_id"], "full": True}))
await tools["ga_stop"].call({"agent_id": aid2})        # 或 mgr.stop(aid2)
```

停止路径的分层探针同上：每步之后轮询
`manager._sessions[aid].process_handle.returncode`，并用 `Get-CimInstance Win32_Process`
按命令行核对真实进程（见 §4.4）。

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

- 哨兵：`ACP_GA_OK_9c7d883e`（本次运行随机生成，8 位 hex）
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
| 哨兵是否匹配 | **匹配**（文件内容含该哨兵；模型这次自己多加了一个行尾换行，上一轮无修复时是 18 字节、无换行） |
| 端到端耗时 | 7.9 s（含 GA 启动 + 两次 LLM 调用） |
| SSE 事件 | 21 条，类型只有 `subagent_start` / `subagent_event` / `subagent_end`（与 P2 子代理同一套） |

> 关于字节差异：任务书要求「内容严格为这一行」，GA 两次运行分别写成 18 B（无换行）与
> 19 B（带一个 `\n`）。这是 **GA 侧模型行为**（`file_write` 原样收到它给的 content），
> 不是客户端问题；验收判据是「文件存在且含哨兵」，两次都满足。

---

## 4. 场景二：长任务与停止 —— ✅ 分层通过

- 任务书：要求 GA 用 `code_run` 跑 `python -c "import time; time.sleep(60)"`，跑完回 DONE。
- 时序：`ga_spawn` → 等 5 s → `ga_stop`（= `manager.stop`）→ 观察进程句柄 `returncode`
  → 再走产品真实停止顺序的硬杀兜底。

### 4.1 单层 `stop()`：只取消 prompt，不杀进程（设计如此）

| 观测点 | 实际值 |
|---|---|
| `ga_stop` | `{"stopped": ["ga_37016b0e"]}` |
| 会话状态 | `stopped`（不是 `errored`） |
| 句柄 `returncode`（stop 后 10 s，0.5 s 轮询 20 次） | **始终 `None`** |
| CIM（按命令行） | `25868,34100`——两条 bridge 进程都还在 |
| `tasklist \| findstr genericagent_acp_bridge` | `<空>`（假阴性，见 §4.4） |

这是**设计**：设计文档 §9.4 写明「优雅路径：对每个会话发 `session/cancel`，等 prompt
响应最多 2s；**返回则正常收尾，不杀进程**」。真 GA 对 `session/cancel` 响应极快
（bridge stderr：`Abort current task...` / `User aborted the task.`），2 s 宽限内 prompt
就收尾了，**没走到 `kill_tree_now` 分支**；bridge 进程留着等下一次 prompt（`ga_send` 复用）。

### 4.2 产品真实顺序的硬杀兜底：进程被回收

按设计 §9.4 的顺序：`acp_manager.stop_generation()` → `process_registry.stop_generation(...)`。

| 观测点 | 实际值 |
|---|---|
| `process_registry.stop_generation()` | 杀掉 **2** 个租约（本轮两个仍在开的 GA 会话），0.33 s |
| `returncode` | **`1`**（首次轮询即非 None） |
| 10 s 内回收 | **是** |
| CIM（按命令行） | `<空>`：无残留 bridge 进程 |
| 会话状态 | `stopped` |

### 4.3 收尾路径 `close_all()`（本轮结束）

| 观测点 | 实际值 |
|---|---|
| `close_all()` | `ok` |
| CIM 复核 | `<空>`（无残留） |

> 早前一轮（manager API 直调、非工具层）测得：单层 `stop()` 后 `returncode` 仍 `None`，
> `close_all()` 后 `returncode=0`、0.33 s 回收；与本节结论一致。

### 4.4 `tasklist | findstr genericagent_acp_bridge` 复核（**假阴性**）

先给结论：**这条命令对 GA 恒为空，不能用它判断 GA 是否停掉。**

原因：`tasklist` 只列**镜像名**（`python.exe`），输出里根本没有命令行；GA bridge 是
「用 python 跑一个 .py」，镜像名永远是 `python.exe`。三次实测：

| 时刻 | `tasklist \| findstr /i genericagent_acp_bridge` | 真实情况（CIM 按命令行） |
|---|---|---|
| GA 正在跑（pid 25868 / 34100，`returncode` 仍 None） | `<空>` | `25868,34100` |
| `ga_stop` 之后（10 s 轮询期间） | `<空>` | `25868,34100`（仍在，符合 §4.1） |
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

## 5. E2E 期间发现并修复的缺陷（预检探针被 stdout 重定向击穿）

### 5.1 缺陷（复跑前的形态）

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

### 5.2 修复（commit `78c0b4f`，非本任务提交）

`78c0b4f fix(acp): resolve the bridge path without importing it`：

- 探针改为 **`importlib.util.find_spec(<module>).origin`** ——只解析模块路径、**不执行目标
  模块**，因此不会再触发 bridge 的 stdout 重定向；同时保留了「没装 / 装错 Python / 父包缺失」
  在 spawn 之前就判出来的能力（`find_spec` 找不到时返回 `None`，实现里补了明确的错误文案）。
- 同步更新 `backend/app/acp/manager.py` 的 docstring 与注释，说明为什么**不能** import。
- 新增回归测试（`backend/tests/test_acp_manager.py`，+39 行），覆盖「bridge import 会重定向
  stdout」这一副作用——原有假模块测试抓不到。

### 5.3 复验（本节数据来自修复后的全新进程）

| 观测点 | 结果 |
|---|---|
| `preflight()` | 成功，0.047 s |
| 返回路径 | `C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py` |
| SHA256 | `4e95c64bc64c1d0884ea9b70dcc8c25be9a4cb401c1893cdbc5d03be2491d6a5` |
| 工具注入 | 5 个 `ga_*` 全部注入，`ga_spawn` 正常开会话 |

即：**预检成功形态已在本机真 GA 上验证过**，不再依赖任何缓存预热。

---

## 6. 未覆盖项与已知限制

| 项 | 状态 | 说明 |
|---|---|---|
| Web UI E2E | **未跑** | 浏览器 + 登录流超出本工作树范围；本记录只到 manager / 工具层 |
| GA 内部 subagent 事件 | **未透出** | GA 自己的子代理（workflow / child_agent）事件不会变成 `subagent_*`；ACP 只把 `session/update` 的 `agent_message_chunk` / `tool_call*` 映射出来，外层只能看到 GA 一层 |
| 跨轮续跑 | **不支持** | 会话按 generation 生命周期管理，本轮结束 `close_all()`；bridge `agentCapabilities.loadSession=false`，无法跨轮恢复 GA 侧上下文 |
| `session/set_mode` | 未触发 | bridge 默认就是 `full_access`，与本次配置相同，该分支未被真实执行 |
| 硬杀兜底（2 s 未响应） | 未在真 GA 上触发 | 真 GA 对 `session/cancel` 响应 < 0.2 s，`kill_tree_now` 分支由单测覆盖，真机未命中；本记录验证的是**在它之后的** `process_registry` 硬杀 |
| GA 模型 / 计费 | 已发生 | GA 用自己的 `llm.yaml`（18 profiles），每轮会话至少 2 次 LLM 调用，属设计 §11.2 已接受的「两套计费」 |
| `INNER_ACP_*` 其余开关 | 未逐一验证 | 只验证了 `INNER_ACP_GA_PYTHON` + `INNER_ACP_MODE=full_access` 这一组 |

---

## 7. 结论

1. **ACP 栈对真 GA 是通的**：预检（`find_spec`）→ 工具注入（5 个）→ 握手 → `session/new`
   → `session/prompt` → `session/update` 流式 → `file_write` 真落盘 → `stopReason: end_turn`。
2. **停止是分层的**：优雅层（`ga_stop`/`stop`）只取消 prompt、不杀进程（设计如此）；
   真正回收靠收尾 `close_all()` 或产品停止顺序里的 `process_registry` 硬杀（`returncode=1`）。
3. **主要缺陷已闭环**：预检探针在真机失效的问题由 `78c0b4f` 用
   `importlib.util.find_spec(...).origin` 修掉，并补了带 stdout 重定向副作用的回归测试；
   §3/§4 的主要数据都在修复后的全新进程里取得，无缓存预热。
4. **剩余边界清楚**：Web UI E2E、GA 内部 subagent 事件、跨轮续跑尚未覆盖（见表 §6）。

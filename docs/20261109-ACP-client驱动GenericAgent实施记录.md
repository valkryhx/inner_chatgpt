# ACP Client 驱动 GenericAgent —— 实施记录（2026-11-09）

> 一句话摘要：Task 1–11 的 ACP 客户端在本机真 GA 上做了 **manager 级 E2E**。
> 哨兵任务（GA 用 `file_write` 写文件）**通过**；停止路径按**产品真实顺序**
> （`acp_manager.stop_generation` → `process_registry.stop_generation`）验证**通过**。
> 同时发现一个**阻塞级产品缺陷**：`AcpSessionManager.preflight()` 的探针被真 GA bridge
> 的 stdout 重定向击穿，真实环境下预检恒失败。**按 Task 12 约束只记录、未改产品代码。**

> 日期：2026-11-09 · 状态：E2E 通过（含 1 个阻塞缺陷待修）· 设计文档：
> `docs/20261109-ACP-client驱动GenericAgent设计与实施.md`

---

## 1. 环境与事实锚定

| 项 | 值 |
|---|---|
| 工作树 | `D:/git_codes/inner_chatgpt/.worktrees/acp-client-ga`（分支 `feature/acp-client-ga`） |
| 宿主 | Windows 11 26200，Git Bash / PowerShell 7 |
| 解释器 | `C:/anaconda3/python.exe`（3.12.4，Anaconda） |
| GA bridge | `C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py` |
| bridge SHA256 | `4e95c64bc64c1d0884ea9b70dcc8c25be9a4cb401c1893cdbc5d03be2491d6a5` |
| `agentInfo.version` | `0.1.0`（`agentInfo.name` = `genericagent-acp`，`title` = `GenericAgent`） |
| GA 模型配置 | `%APPDATA%/GenericAgent/llm.yaml`（18 个 profile，active=#16；**内容未读取、未落盘**） |
| 验收级别 | **manager 级**（直接构造 `AcpSessionManager`），不是 Web UI E2E |

为什么不是浏览器 E2E：本任务明确「浏览器 + 登录流超出本工作树范围」，因此把整条 ACP 栈
（transport → client → session → manager）在进程内按真实调用顺序跑通，事件进自建的
最小 recorder（只有 `put()`，与 `EventBuffer` 同签名）。

---

## 2. 复现方式

```bash
cd /d/git_codes/inner_chatgpt/.worktrees/acp-client-ga

# 预检事实（解释器 + bridge 路径 + 摘要）
C:/anaconda3/python.exe -c "import frontends.acp.genericagent_acp_bridge as b; print(b.__file__)"
python -c "import hashlib;print(hashlib.sha256(open(r'C:/anaconda3/Lib/site-packages/frontends/acp/genericagent_acp_bridge.py','rb').read()).hexdigest())"
```

三个验证脚本是**一次性 throwaway**（放在 gitignored 的 `work/`，按任务要求跑完即删，**未入库**）。
按下面的骨架可原样重建：

```python
import asyncio, sys
sys.path.insert(0, "backend")
from app.acp import manager as manager_mod
from app.acp.config import load_acp_settings
from app.acp.manager import AcpSessionManager
from app.subagent.context import AgentContext

class Recorder:                      # 只用到 put()，与 EventBuffer 同签名
    def __init__(self): self.events = []
    def put(self, ev): self.events.append(ev)

ctx = AgentContext(user_id="e2e", session_id="s1", message_id="m1", generation_id="g1")
settings = load_acp_settings({"INNER_ACP_GA_PYTHON": "C:/anaconda3/python.exe",
                              "INNER_ACP_MODE": "full_access"})
# ⚠️ 绕过 §5.1 的阻塞缺陷：预热预检成功缓存（产品代码不改）
manager_mod._PREFLIGHT_CACHE[tuple(settings.command)] = "<bridge 绝对路径>"
mgr = AcpSessionManager(ctx=ctx, buffer=Recorder(), settings=settings)

spawned = await mgr.spawn(task="请使用 file_write 工具在当前工作目录创建文件 acp_ga_e2e.txt，"
                               "文件内容严格为这一行：ACP_GA_OK_<8位hex>。"
                               "不要询问用户，不要写别的内容。完成后只回复 DONE。", timeout_s=600)
await mgr.wait(spawned["agent_id"], timeout_s=600)
print(await mgr.result(spawned["agent_id"]))
```

停止路径的三层探针同理：`stop()` / `close_all()` / `stop_generation()+process_registry.stop_generation()`，
每步之后轮询 `mgr._sessions[aid].process_handle.returncode`，并用
`Get-CimInstance Win32_Process` 按命令行核对真实进程（见 §4.4）。
本次运行的原始 JSON 证据保留在本地 `work/*.json`（同样未入库）。

---

## 3. 场景一：哨兵任务（真 GA 写文件）—— ✅ 通过

- 哨兵：`ACP_GA_OK_d9834466`（本次运行随机生成，8 位 hex）
- 任务书（原样）：

```text
请使用 file_write 工具在当前工作目录创建文件 acp_ga_e2e.txt，文件内容严格为这一行：ACP_GA_OK_d9834466。不要询问用户，不要写别的内容。完成后只回复 DONE。
```

| 观测点 | 实际值 |
|---|---|
| `spawn()` | `{"agent_id": "ga_cbbd4df0", "status": "running"}` |
| `wait(timeout_s=600)` | `timed_out: false`，`status: completed`，`last_seq: 10` |
| `result()` | `status: completed`、`prompts: 1`、`tool_calls: 1`、`stop_reason: end_turn`、`error: ""` |
| 工具步骤 | 1 步：`tool_start` name=`file_write`，rawInput `{"path": "acp_ga_e2e.txt", "content": "ACP_GA_OK_d9834466"}` → `tool_result` `{"status": "success", "writed_bytes": 18}` |
| 产物文件 | `backend/data/workspaces/e2e/acp_ga_e2e.txt`，**18 字节、无结尾换行**，`od -c` 逐字节等于哨兵 |
| 哨兵是否匹配 | **匹配**（`ACP_GA_OK_d9834466` 原样落在文件里） |
| 端到端耗时 | 10.0 s（含 GA 启动 + 两次 LLM 调用） |
| SSE 事件 | 20 条，类型只有 `subagent_start` / `subagent_event` / `subagent_end`（与 P2 子代理同一套） |

GA 侧 stderr 佐证（节选，`stderr_tail`）：`initialize` 返回 `protocolVersion: 1` +
`agentInfo.version: 0.1.0`；`session/new` 返回 `currentModeId: full_access`；prompt 结束
`{"stopReason":"end_turn"}`。**`INNER_ACP_MODE=full_access` 与 bridge 默认模式一致，未触发 `session/set_mode`。**

---

## 4. 场景二：长任务与停止 —— ⚠️ 分层结论（产品路径通过，单层 `stop()` 按设计不杀进程）

- 任务书：要求 GA 用 `code_run` 跑 `python -c "import time; time.sleep(60)"`，跑完回 DONE。
- 时序：`spawn` → 等 5 s → `mgr.stop(agent_id)` → 观察进程句柄 `returncode`。

### 4.1 单层 `mgr.stop()`（任务书要求的判据）

| 观测点 | 实际值 |
|---|---|
| `stop()` 返回 | `{"stopped": ["ga_3f1a1d3a"]}`，耗时 0.2 s |
| 状态 | `stopped`（不是 `errored`），`stop_reason: end_turn` |
| 句柄 `returncode`（stop 后 10 s 内，0.5 s 轮询 20 次） | **始终 `None`** |
| 外部进程视图（CIM 按命令行） | bridge pid `25100` **仍在** |
| 判据结论 | **未满足**「`returncode` 10 s 内变非 None」 |

**这不是泄漏，是设计**：`docs/20261109-ACP-client驱动GenericAgent设计与实施.md` §9.4 写明
「优雅路径：对每个会话发 `session/cancel`，等 prompt 响应最多 2s；**返回则正常收尾，不杀进程**」。
真 GA 对 `session/cancel` 响应极快（stderr：`Abort current task...` / `User aborted the task.`），
于是 `_stop_entry` 的 2 s 宽限内 prompt 就收尾了，**根本没走到 `kill_tree_now` 分支**，
bridge 进程留着等下一次 prompt（`ga_send` 复用同一会话）。

### 4.2 收尾路径 `close_all()`（本轮结束）

| 观测点 | 实际值 |
|---|---|
| `close_all()` 耗时 | 0.33 s |
| `returncode` | `0` |
| CIM 外部进程视图 | 无 `genericagent_acp_bridge` 进程残留 |

### 4.3 产品真实停止顺序（`/stop` 的链路）

按 §9.4 的顺序复跑：`acp_manager.stop_generation()` → `process_registry.stop_generation(...)`。

| 观测点 | 实际值 |
|---|---|
| `acp_manager.stop_generation()` | 停 1 条会话，0.17 s；`returncode` 仍 `null`（同上，优雅层不杀） |
| `process_registry.stop_generation()` | 杀掉 1 个租约，0.17 s；`returncode` 立刻变 `1` |
| 10 s 内回收 | **是**（首次轮询即非 None） |
| 卡片状态 | `stopped` |

即：**单看 `mgr.stop()` 会误判「进程没停」，但产品真实顺序会把进程清掉**。
任务书给的判据只覆盖了优雅层，故这里按事实分三层记录，不改判据结论。

### 4.4 `tasklist | findstr genericagent_acp_bridge` 复核

先给结论：**这条命令对 GA 恒为空，是假阴性，不能用它判断 GA 是否停掉。**

原因：`tasklist` 只列**镜像名**（`python.exe`），命令行列根本不在输出里；GA bridge 是
「用 python 跑一个 .py」，镜像名永远是 `python.exe`。实测（`work/acp_ga_tasklist_probe.py`）：

| 时刻 | `tasklist \| findstr /i genericagent_acp_bridge` | 真实情况（CIM 按命令行） |
|---|---|---|
| GA 正在跑（pid 35696，`returncode` 仍 None） | `<空>` | 该 pid 存活 |
| `mgr.stop()` 之后 2 s | `<空>` | 该 pid **仍存活**（§4.1） |
| `close_all()` 之后 | `<空>` | 已退出（`returncode=0`） |

（Git Bash 里直接写 `findstr /i ...` 会被 MSYS 把 `/i` 当路径，报
`FINDSTR: 无法打开 i`；上面的结果都经 `cmd /c` 执行，才是 `findstr` 的真实输出。）

能区分「在跑 / 停了」的等价复核是**按命令行过滤**：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*genericagent_acp_bridge*' } |
  ForEach-Object { $_.ProcessId }
```

| 时刻 | pid 列表 |
|---|---|
| 空闲（任何一次运行前） | 空 |
| 长任务运行中 | `25100`（`work/stop_probe.json`）、`35696`（`work/tasklist_probe.json`） |
| 单层 `mgr.stop()` 之后 10 s 内 | `25100`（仍在，符合 §4.1 的设计） |
| `close_all()` / `process_registry` 硬杀之后 | 空 |

另：全部运行结束后 `Get-Process -Id 22548,23764`（两次整跑的 bridge pid）均查不到，无残留。

---

## 5. 发现的缺陷（阻塞级，按 Task 12 约束**只报告、未修复**）

### 5.1 `preflight()` 的探针被真 GA bridge 的 stdout 重定向击穿

**现象**：按设计文档 §11.3 的形式跑预检，真实 GA 环境**恒失败**：

```text
$ python work/acp_ga_e2e.py    # 未预热缓存时
AcpUnavailableError: GenericAgent ACP bridge 不可用: C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py
```

**直接复算**（与 `build_child_env` 完全同一份白名单环境）：

```text
command = ['C:/anaconda3/python.exe', '-m', 'frontends.acp.genericagent_acp_bridge']
rc = 0
STDOUT = ''
STDERR = 'C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py\n'
```

即：**探针的 `print(b.__file__)` 没进 stdout，而是进了 stderr**；`manager.preflight()` 只看
`stdout` 且要求非空，于是把「bridge 完全可用」判成「不可用」。

**根因**：`genericagent_acp_bridge.py:11-45` 在模块 import 期就把 stdout 抢走了——
Windows 下 `os.dup2(sys.stderr.fileno(), sys.__stdout__.fileno())`（**fd 1 本身被重定向**），
再 `sys.stdout = _StdoutToStderrRouter()`（文本层再兜一层）。这是 bridge 的**正确设计**
（JSON-RPC 通道要独占 stdout），但正好让「靠 print 取路径」的探针失效。

**为什么是阻塞级**：`AcpSessionManager.spawn()` 第一行就 `self.preflight()`（`manager.py:240`），
所以真实环境下 `ga_spawn` 永远拿不到会话；按设计 §11.3，预检失败时整个 `ga_*` 工具集不注册。
**等于 ACP 能力在真机上是不可用的**，而单测用的是假模块（不重定向 stdout），所以一直全绿。

**设计文档自身的矛盾**：§3.3（`docs/20261109-ACP-client驱动GenericAgent设计与实施.md:120-121`）
已经写明「GA bridge 已做 `msvcrt.setmode(..., O_BINARY)` 与 stdout fd 重定向」，
§11.3 的探针却仍用 `print(b.__file__)`——事实记录到了，实现没跟着改。

**建议修法**（本任务不实施，供下一个任务选）：

1. 探针改读 stderr：`-c "import ... as b, sys; sys.stderr.write(b.__file__)"`；
2. 或探针写独立 fd / 临时文件，绕开被抢的 fd 1；
3. 或干脆不 import：`-c "import importlib.util as u; print(u.find_spec('frontends.acp.genericagent_acp_bridge').origin)"`
   （`find_spec` 只 import 父包，不执行 bridge 模块体，天然不受重定向影响）；
4. 修完必须补一条**带 stdout 重定向副作用**的回归测试（现有假模块测试抓不到这个 bug）。

**本次 E2E 如何绕过**：验证脚本在调 `preflight()` 前把已知路径写进
`app.acp.manager._PREFLIGHT_CACHE[tuple(settings.command)]`（模块级成功缓存），
于是 `preflight()` 走缓存分支、`spawn()` 正常继续。**产品代码一行未改**；
这也意味着「预检本身」在本记录里**只验证了失败形态，没有验证成功形态**。

### 5.2 非缺陷：单层 `stop()` 不杀进程

见 §4.1。设计如此（§9.4「返回则正常收尾，不杀进程」），只是任务书给的判据只看这一层。
产品真实顺序（§4.3）会清干净，**不作为缺陷记录**。

---

## 6. 未覆盖项与已知限制

| 项 | 状态 | 说明 |
|---|---|---|
| Web UI E2E | **未跑** | 浏览器 + 登录流超出本工作树范围；本记录只到 manager 层 |
| `preflight()` 成功形态 | **未验证** | 被 §5.1 的缺陷挡住，只能验证失败形态 |
| GA 内部 subagent 事件 | **未透出** | GA 自己的子代理（workflow / child_agent）事件不会变成 `subagent_*` 事件；ACP 只把 `session/update` 的 `agent_message_chunk` / `tool_call*` 映射出来，外层只能看到 GA 的一层 |
| 跨轮续跑 | **不支持** | 会话按 generation 生命周期管理，本轮结束 `close_all()`；bridge 的 `loadSession: false`，`agentInfo.agentCapabilities.loadSession=false`，无法跨轮恢复 GA 侧上下文 |
| `session/set_mode` | 未触发 | bridge 默认就是 `full_access`，与本次配置相同，该分支未被真实执行 |
| 硬杀兜底（2 s 未响应） | 未在真 GA 上触发 | 真 GA 对 `session/cancel` 响应 < 0.2 s，`kill_tree_now` 分支由单测覆盖，真机未命中 |
| GA 模型/cost | 已发生 | GA 用自己的 `llm.yaml`（18 profiles），本次 2 次会话各至少 2 次 LLM 调用，属设计 §11.2 已接受的「两套计费」 |
| `INNER_ACP_*` 其余开关 | 未逐一验证 | 只验证了 `INNER_ACP_GA_PYTHON` + `INNER_ACP_MODE=full_access` 这一组 |

---

## 7. 结论

1. **ACP 栈对真 GA 是通的**：握手 → `session/new` → `session/prompt` → `session/update` 流式
   → `file_write` 真落盘 → `stopReason: end_turn`，全链路在 manager 层跑通（10 s）。
2. **停止是分层的**：优雅层（`stop`）只取消 prompt、不杀进程；真正回收靠收尾
   （`close_all()`，`returncode=0`）或产品停止顺序里的 `process_registry` 硬杀（`returncode=1`）。
3. **一个阻塞缺陷**：`preflight()` 的探针在真机恒失败（§5.1）。**修它之前，真实部署里
   `ga_*` 工具集不会注册**——这是本次最重要的结论，优先级高于本记录的其他内容。

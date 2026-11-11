# workspace 沙箱逃逸（code_run 只拦写不拦读）：安全缺陷与修复方案

> 日期：2026-11-11　状态：**已实施并验证**
> 起因：真机发现子代理 `file_*` 看不到仓库，追查时发现更严重的问题

## 1. 起因：一个「看得到、读不到」的现象

真机 runId `145738-1fee` 里，`slowpoke` 的 `file_glob` 返回「不是目录」、
绝对路径被越界校验拒绝 —— 因为它的 workspace 根是空目录
`backend/data/workspaces/<uid>`，看不到仓库里的 `backend/app`。

原本以为这只是「要不要把仓库暴露进 workspace」的产品选择。**追查后发现不是。**

## 2. 真正的问题：`code_run` 的沙箱只拦写，不拦读

### 2.1 实测（`u_attacker` 身份，全新用户）

```text
D:/.../backend/data/app.db        exists=True  READ=OK
D:/.../backend/data/logs/app.log  exists=True  READ=OK
WRITE=BLOCKED PermissionError          ← 写确实被拦住了
```

用 `code_run` 读 `app.db` 能拿到：

| 读到什么 | 实例 |
|---|---|
| **其他用户的 LLM api_key** | `('u_f6ac05682e44','my-ds',...,'sk-pYE4Wg1u8')` 等多条 |
| **用户的密码哈希** | `('u_f6ac05682e44','alice','$2b$12$yZ5ft4u')` |
| **其他用户的全部消息** | 按 `user_id` 分组计数，可逐条读取 |

对照：`file_read` / `file_glob` 对同一路径**都正确拒绝**了：

```text
[工具执行错误] 路径越界（必须位于 workspace 或 skill 目录内）
```

### 2.2 根因

`backend/app/run_guard.py` 的 `WORKSPACE_PYTHON_GUARD` 只审计**写操作**：

```python
def _audit(event, args):
    if event == "open":
        if len(args) >= 3 and _open_is_write(args[1], args[2]):   # ← 只查写
            _check(args[0])
    elif event in {"os.mkdir", "os.rmdir", "os.remove", ...}:
        ...
```

`_open_is_write()` 判定的是 `mode` 里有没有 `w/a/x/+`。以 `"r"` 或 `"rb"` 打开的
**读操作完全不检查路径**。于是：

| 工具 | 读边界 | 写边界 |
|---|---|---|
| `file_read` / `file_glob` / `file_grep` | workspace + skill 目录（严格） | — |
| `code_run`（python） | **无限制** | workspace（严格） |
| `code_run`（shell） | **无限制**（`type` / `cat` 直接读） | 无审计（见 §2.3） |

### 2.3 shell 分支的问题更严重

`code_run` 的 `powershell` / `bash` 分支**完全不经过 `WORKSPACE_PYTHON_GUARD`**：

```python
elif code_type in ("powershell", "ps1", "pwsh", "bash", "sh", "shell"):
    cmd = (["powershell", ...] if os.name == "nt" else ["bash", "-c", code])
```

读写都没有审计。只是 `cwd` 落在 workspace 里而已 —— 换到任意绝对路径即可。

### 2.4 影响面

**多用户隔离是本项目的第一条能力**（README §1：「会话/消息/模型/MCP/Skill 全部按
`user_id` 隔离，越权返回 404」）。而 `code_run` 把这条隔离绕过去了：

- 任何登录用户都能读出所有其他用户的 api_key（可直接盗用额度）、密码哈希
  （可离线爆破）、全部对话内容；
- 子代理继承同样的工具，所以「派一个 teammate」也能做到；
- A2A 的 `full` 档 token 同样能调用 `code_run`。

这不是「要不要暴露仓库」的功能问题，是**沙箱逃逸**。

### 2.5 对标 Aivory：它怎么解决的

参考 `D:/git_codes/Aivory/sandbox-service`（同为多租户产品，有等价的代码执行工具）。

**做法：每次会话起一个 Docker 容器，容器就是隔离边界。**

```python
args = [
    "run", "-d",
    "--network", "none",              # 无网络
    "--memory", MEMORY, "--memory-swap", MEMORY,
    "--cpus", CPUS, "--pids-limit", PIDS_LIMIT,
    "--user", "1000:1000",            # 非 root
    "--security-opt", "no-new-privileges",
    "--cap-drop", "ALL",
    "--read-only",                    # 根文件系统只读
    "--tmpfs", "/tmp:...,size=...",
    "--tmpfs", f"{WORKSPACE}:...,mode=1777",
]
```

关键点：**容器里根本没有宿主机的文件系统**。用户的 Python 跑在容器内，
所以「读宿主机 `/data/app.db`」这件事在物理上不可能 —— 不需要审计钩子，
也不需要维护「可读根白名单」。

**更关键的一条**：Aivory 在没有配置 sandbox 时**直接报错拒绝执行**：

```go
return fmt.Errorf("sandbox: no SANDBOX_BASE_URL configured")
```

它没有「降级到宿主机跑」的分支。这是两者最本质的差别。

| | Aivory | Hlaudz（当前） |
|---|---|---|
| 隔离边界 | Docker 容器 | 进程内 Python 审计钩子 |
| 读宿主机 | 物理不可能 | **完全放开** |
| 写宿主机 | 容器内不可写 | 审计钩子拦截（有效） |
| shell 分支 | 同在容器内，同样受限 | **完全绕过守卫** |
| 无沙箱时 | 拒绝执行 | 照常执行 |

### 2.6 Hlaudz 本次的修复选择

完整上 Docker 是正解，但那是架构级改动（部署形态、镜像、跨平台、Windows 开发环境
都要处理），不该塞进这次修复。本次采用**进程内最小可行**方案：

1. 审计钩子**同时管读**：`open` 无论读写都校验，读的可读根 = workspace + skill 目录
   （与 `file_read` 的 `safe_read_roots` 完全一致）；
2. **显式拒绝敏感路径**：`app.db` 及其 `-wal`/`-shm`、其他用户的 workspace 目录；
3. **目录遍历事件**（`os.listdir`/`os.scandir`/`os.walk`）同样校验；
4. **shell 分支**同样装守卫（用 `sitecustomize` 注入，见 §3.2）。

> 明确的局限：进程内审计钩子**不是**安全边界。有心人可以绕过
> （`ctypes` 直接调 libc、`subprocess` 起一个新的 python 不走守卫）。
> 所以文档里必须写清楚：**这是防御纵深的一层，不是隔离保证**；
> 真正多租户上生产需要按 Aivory 的路子换成容器/gVisor/microVM。

## 3. 修复方案（本次实施范围）

### 3.1 原则

1. **读也要管**：`code_run` 的读边界必须与 `file_read` 一致（workspace + skill 目录），
   否则两个工具对「能读什么」的判断不一致，弱的那条就是漏洞。
2. **shell 分支不能豁免**：进程级沙箱做不到（跨平台代价太高），所以要么限制
   shell 的能力，要么把它降级为「只能操作 workspace 内相对路径」并以文档明确
   其边界。**P0 先做最保守的：默认禁用绝对路径的 shell 读取**。
3. **不破坏正常用法**：读 workspace 内的文件、跑 workspace 内的脚本必须照常工作。

### 3.2 具体改动

| # | 改动 | 位置 |
|---|---|---|
| 1 | `_audit` 增加**读**路径检查：`open` 事件无论读写都校验，只读时校验可读根集合 | `run_guard.py` |
| 2 | 守卫接收「可读根列表」（workspace + skill 目录），而不是只有一个 write root | `run_guard.py` + `builtin_tools.py` |
| 3 | `os.listdir` / `os.scandir` / `os.walk` 等目录遍历事件也要校验 | `run_guard.py` |
| 4 | shell 分支：默认拦截含绝对路径的代码，或在环境变量里显式放开 | `builtin_tools.py` |
| 5 | 补实测用例：以用户 A 身份读 `app.db` 必须失败 | `tests/` |

### 3.3 为什么不「把仓库暴露进 workspace」

那会让每个用户的 workspace 都复制一份仓库，既浪费磁盘又带来「改了别人的代码」的
一致性问题。原问题的正确解法是：**需要读代码的任务，由管理员显式配置一个只读根**
（下面的 P1），而不是默认放开。

## 4. 实施顺序

| 步 | 内容 | 验证 |
|---|---|---|
| 1 | 守卫支持读根校验（python 分支） | 单测：workspace 内读 OK；`app.db` 读失败；skill 目录读 OK |
| 2 | 目录遍历事件校验 | 单测：`os.listdir('D:/...')` 失败 |
| 3 | shell 分支收口 | 单测：绝对路径 shell 读取被拒；相对路径正常 |
| 4 | 端到端实测 | 以新建用户身份跑上面 §2.1 的探针，必须全部失败 |
| 5 | 回归 | 全量单测 + 现有 e2e |

## 5. P1（本次不做，写清边界）

| 能力 | 说明 |
|---|---|
| **只读根配置**（`HLZ_READONLY_ROOTS`） | 管理员显式配置「额外可读目录」，用于「让 agent 读本仓库源代码」这类真实需求 |
| 子代理只读根继承 | 子代理继承父代理的只读根，而不是只能看自己的 workspace |

## 6. 风险

| 风险 | 缓解 |
|---|---|
| 拦读破坏现有用法（如 `load_skill` 读内置 skill） | 可读根包含 workspace + 用户 skill + 内置 skill + 公用 skill（与 `safe_read_roots` 完全一致） |
| 拦 `os.walk` 影响性能 | 只在路径**越界**时抛错，正常遍历零开销 |
| shell 收口后某些合法脚本跑不了 | 用 `HLZ_CODE_RUN_SHELL_ABS=1` 显式放开，并在提示词里说明边界 |
| 跨平台行为不一致（Windows vs POSIX） | 守卫用 `pathlib.Path.resolve()`，两平台同一套逻辑 |

## 7. 变更记录

- 2026-11-11：首版。基于 `u_attacker` 身份的真实探针实测（可读出其他用户的
  api_key / 密码哈希 / 全部消息），与 `file_read` 的对照结果。
- 2026-11-11：补 §9 对标 LibreChat（`D:/git_codes/LibreChat`）。

## 8. 实施记录（已完成）

### 8.1 改动

| 文件 | 改动 |
|---|---|
| `backend/app/run_guard.py` | 审计钩子**读写都校验**；新增 `sqlite3.connect` 拦截；目录遍历（`os.listdir`/`os.scandir`）校验；解释器自身目录与脚本目录自动放行；新增 `read_roots_for()`（与 `workspace.safe_read_roots` 共用同一份定义）、`guard_argv()`、`shell_code_reason()` |
| `backend/app/builtin_tools.py` | 用 `guard_argv()` 传可读根；脚本落到 `workspace/.tmp`（不再用系统 Temp，避免把共享 Temp 加进可读根）；shell 分支加静态检查；`workspace_env(user_id=)` 传可读根 |

### 8.2 实测（修复前 vs 修复后，同一段代码，用户 `u_demo`）

| 攻击/用法 | 修复前 | 修复后 |
|---|---|---|
| `open(".../data/app.db","rb")` | **读到 `sk-pYE4Wg1u8jOtMxiky...`** | `PermissionError: 是平台数据库文件，不允许读取` |
| `sqlite3.connect(".../data/app.db")` | 同上（能查 api_key / 密码哈希） | 被 `sqlite3.connect` 审计事件拦下 |
| `os.listdir(".../workspaces/u_attacker")` | 成功 | `PermissionError: 只能读取当前 workspace` |
| `open(".../backend/app/config.py")` | 成功 | `PermissionError` |
| `os.listdir("../../..")` | 成功 | `PermissionError` |
| `os.listdir(".../data/logs")` | 成功 | `PermissionError` |
| workspace 内读写 | OK | **OK**（不破坏） |
| 纯计算 / numpy / 标准库 | OK | **OK**（不破坏） |

### 8.3 测试

`backend/tests/test_code_run_read_sandbox.py`（17 条）：

- 必须拦住：平台 DB（`open` 与 `sqlite3.connect` 两条路径）、他人 workspace、
  仓库源码、目录穿越、服务端日志；
- 不能破坏：workspace 内读写、纯计算、第三方 import、读自己的文件；
- shell 分支：绝对路径 / 目录穿越 / 数据库文件名一律拒绝，相对路径放行；
- 一致性：`read_roots_for()` 必须等于 `workspace.safe_read_roots`。

后端全量 **636 → 653**。三组变异反向确认 RED（读校验 / sqlite3 拦截 / 目录遍历）。

### 8.4 已知局限（必须写清楚，不能假装解决了）

**进程内审计钩子不是安全边界。** 至少还有这些绕过路径：

| 绕过方式 | 说明 |
|---|---|
| `ctypes` 直调 `open`/`CreateFileW` | 不走 Python 层，审计钩子看不到 |
| `subprocess` 起一个新的 python | 新进程不继承 `sys.addaudithook`（除非它自己装） |
| shell 分支的自定义写法 | 静态检查只能拦常见形态（`$(...)`、变量拼接可绕过） |
| `os.system` / `os.popen` | 子进程同样不受钩子约束 |

**结论**：本次修复拦住了「模型顺手写一段脚本读数据库」这类**真实会发生**的情况
（真机上确实发生了），但**不能**作为多租户安全的最终答案。

真正要上生产多租户，必须按 Aivory 的路子换隔离边界：

```python
# Aivory sandbox-service/app.py 的做法
["docker", "run", "-d",
 "--network", "none", "--read-only", "--cap-drop", "ALL",
 "--user", "1000:1000", "--security-opt", "no-new-privileges",
 "--memory", MEMORY, "--pids-limit", PIDS_LIMIT, ...]
```

而且**没配 sandbox 时直接拒绝执行**（`return fmt.Errorf("sandbox: no SANDBOX_BASE_URL configured")`），
不退回宿主机 —— 这是与 Hlaudz 当前实现的本质差别。

建议把「code_run 换成容器隔离」列为独立阶段（P1），不在本次范围。

## 9. 对标 LibreChat：把「执行」彻底移出平台进程

> 调研对象：`D:/git_codes/LibreChat`（更工程化的一套，已产品化多租户代码执行）。
> 结论先放在这里：**LibreChat 根本不执行用户代码**。它连「进程内跑一段脚本」这条
> 路都没有，所以 §2 那类逃逸在它的架构里不存在。

### 9.1 它把代码执行整体外置了

`packages/api/src/code/` 有 24 个非测试源文件（另有 24 个 `.spec.ts`）、`workspace.ts`
单独 60 KB，看起来是做代码执行的。
实际上这一层是**控制平面**，只做四件事：协商能力、校验路径、转发请求、映射错误。
真正的执行发生在平台进程之外。

证据链：

| 位置 | 说明 |
|---|---|
| `vendor/code/package.json` | `"librechatUpstream": { "repository": "https://github.com/LibreChat-AI/code-interpreter" }` —— worker 是独立仓库，vendor 里只放了浏览器登记协议 |
| `CONTEXT.md:5` | "the worker's local sandbox and capability flags remain **the final execution ceiling**" —— 隔离由 worker 负责，不由平台负责 |
| `bridge.ts` | 全部是 HTTP 调用：`/bridge/workers/<id>/status`、pairing、lifecycle，`Authorization: Bearer <token>` |
| `grep child_process packages/`（排除测试） | 只有 `agents/hooks/executor.ts` + `reaper.ts`（跑 hooks 声明的命令）、`app/build.ts`、`files/documents/libreoffice.ts` —— **没有一条执行用户提交代码的路径** |
| `librechat.example.yaml` | `environments`: `type: managed`（运维自建 Code API）或 `type: attached`（`remote-bridge` 把活租给外发的 `@librechat/code` worker） |

`attached` 的模型值得注意：worker 是**外发连接**的。用户的机器上跑
`librechat-code pair <endpoint> <code>`，主动连到平台；平台从不主动连用户的机器，
也从不把用户代码拉回自己的进程里跑。

### 9.2 三种隔离路线的工程代价

| 路线 | 代表 | 隔离边界 | 平台进程里有没有用户代码 |
|---|---|---|---|
| 进程内 + 审计钩子 | **Hlaudz（当前）** | 同进程的 Python 审计 | **有** |
| 进程内 + 容器 | Aivory | Docker 容器 | 有（但容器内） |
| 执行整体外置 | **LibreChat** | 外部 worker 自己的沙箱 | **没有** |

LibreChat 这条路线的好处是：**平台进程里根本没有用户代码**，所以「读到平台的
`app.db`」在物理上不成立 —— 和 Aivory 用容器达到同样的效果，但不需要平台自己
管镜像、管 Docker、管跨平台。

### 9.3 最值得抄的一条：能力协商 + fail closed

LibreChat 不假定 worker 有什么能力，**让 worker 自报**（`CodeBridgeWorkerStatus`）：

```ts
export type CodeBridgeWorkerStatus = {
  status: 'offline' | 'starting' | 'ready';
  sandboxProfile?: string;          // 例如 'anthropic-srt'
  runtimes?: string[];              // ['bash']
  operations?: CodeWorkspaceOperation[];   // read_file / write_file / execute_command ...
  maxCommandTimeoutMs?: number;
};
```

平台只根据**自报**的能力决定注入哪些工具（`capabilities.ts`）：

```ts
if (status.status !== 'ready') throw new CodeWorkspaceSelectionError('worker_unavailable');
if (!status.workspaces || !status.operations) throw new CodeWorkspaceSelectionError('unsupported');
```

`supportsProgrammaticCodeExecution()` 更是直接要求：

```ts
status.status === 'ready' &&
status.runtimes?.includes('bash') === true
```

否则返回 `false` —— 对应的 spec 名字就叫 **"fails closed without native workspace
capabilities"**。**没有能力就不注册工具**，不存在「降级到平台进程里跑」。

### 9.4 第二条：路径是「白名单形状」，不是「解析后校验」

`workspace.ts` 的 `isSafePath()` 不从「绝对路径 + 越界判断」入手，而是**先规定合法形状**：

```ts
!value.includes('\') &&          // 反斜杠一律不许（Windows 路径直接出局）
!value.startsWith('/') &&          // 不许绝对路径
!/^[A-Za-z]:/.test(value) &&       // 不许盘符
value.split('/').every((s) => s !== '' && s !== '.' && s !== '..')   // 不许 . / .. / 空段
value.length <= 4096 &&
!value.includes('\0') && !value.includes('\r') && !value.includes('\n')
```

对比 Hlaudz 现在的做法（`resolve()` 之后判断是否在根内）：**LibreChat 的写法
在语义上更安全** —— 它对「什么样的路径算合法」有唯一答案，不需要考虑
`resolve()` 的符号链接语义、大小写、UNC、`\?\` 前缀这些平台差异。
`\?\` 和 UNC 正是绕过 `resolve()` 比较的常见手法，而它们连形状校验都过不去。

### 9.5 第三条：能力上限写死在代码里，不是配置项

`workspace.ts` 顶部全是常量，不经过任何配置：

```ts
const MAX_PATH_LENGTH = 4096;
const MAX_READ_BYTES = 1024 * 1024;
export const WORKSPACE_WRITE_MAX_BYTES = 1024 * 1024;
const MAX_COMMAND_BYTES = 32 * 1024;
const MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024;
export const WORKSPACE_COMMAND_MAX_TIMEOUT_MS = 5 * 60_000;
const WORKSPACE_EDIT_MAX_COUNT = 100;
const MAX_SEARCH_RESULTS = 200;
const MAX_LIST_RESULTS = 500;
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;
```

配置只能**往下压**（`maxCommandTimeoutMs: 120000` 注释写着 "Hard cap: 300000"，
且有 `fitWorkspaceCommandTimeoutToBudget()` 做二次夹取）—— 配置读错了，
天花板还在代码里。

### 9.6 第四条：审批是可枚举的三档，默认最严

`packages/data-provider/src/code/approval.ts`：

```ts
export const CODE_APPROVAL_MODES = ['ask', 'acceptEdits', 'fullAccess'] as const;
const MODE_PERMISSIONS = {
  ask:         { fileWrite: 'ask',   commandExecution: 'ask'   },
  acceptEdits: { fileWrite: 'allow', commandExecution: 'ask'   },
  fullAccess:  { fileWrite: 'allow', commandExecution: 'allow' },
};
```

关键在于这个默认值：

```ts
const permitted = new Set(allowedModes ?? ['ask']);   // ← 没配置就是 ask
```

注释写着 **"Omitted deployment configuration never grants unattended execution."**
—— 不配就是「每次问」，不是「每次放行」。这一点和 Hlaudz 的 `tool_policy`（三档
`full` / `read_only` / `chat_only`，建 token 时定死）思路一致，但 LibreChat 的默认
更保守。

### 9.7 第五条：沙箱能力用「提示词披露」，而不是假装统一

LibreChat 不假设所有 worker 都有沙箱。它把差异写进 **工具描述**：

```ts
/** Native SRT workers make the host filesystem read-only outside the workspace and a private `$TMPDIR`. */
const ATTACHED_WORKSPACE_NATIVE_SANDBOX_SCRATCH =
  '- / and /tmp are read-only; write scratch files to $TMPDIR or the workspace. Programs that hardcode /tmp fail.';
```

有沙箱 → 给模型这段说明；没沙箱 → 给不带这段的版本。模型因此知道
「`/tmp` 能不能写」，不会写出注定失败的脚本。

这比「所有环境假装一样」更诚实，也更容易排障 —— 模型的行为差异有据可查。

### 9.8 三套方案横向对比

| 维度 | Aivory | LibreChat | Hlaudz（本次修复后） |
|---|---|---|---|
| 用户代码在哪跑 | 平台起的 Docker 容器 | **平台进程之外**（外部 worker） | **平台进程内** |
| 隔离手段 | 容器 namespace / cgroup | worker 自己的沙箱（native SRT / WSL2 / Docker / NsJail） | Python 审计钩子 |
| 无沙箱时 | 拒绝执行 | **不注册工具**（fail closed） | 照常执行，靠钩子 |
| 平台能否读到用户数据 | 用户进程读不到平台 | 用户进程与平台进程物理分离 | **同进程，靠钩子拦** |
| shell 分支 | 容器内 | worker 内 | **绕过钩子，靠静态检查** |
| 路径校验 | 容器挂载 | **形状白名单**（禁 `\` / 绝对 / `..`） | `resolve()` 后比根 |
| 能力上限 | 容器资源限制 | **代码内常量 + 配置只能下压** | 部分有 |
| 审批默认 | — | **`ask`（不配就是问）** | `full`（建 token 时定） |
| 跨平台 | 只需容器 | 同一套协议 | 需同时管 Windows / POSIX 两套路径语义 |

### 9.9 对 Hlaudz 的具体建议

本次（§8）已经堵住「模型顺手写脚本读数据库」这条真实路径，但**架构位置没变**：
用户代码仍在平台进程内。按代价从低到高：

| # | 建议 | 代价 | 收益 |
|---|---|---|---|
| 1 | **路径形状校验**（抄 §9.4）：`file_*` 与 `code_run` 统一改成「先验形状，再解析」 | 半天 | 干掉 `\?\`、UNC、盘符这些 `resolve()` 边角 |
| 2 | **能力自报 + fail closed**（抄 §9.3）：`code_run` 启动前探一次沙箱能力，探不到就不注册 | 1 天 | 「没有隔离就不执行」变成默认，不再是靠人记得配 |
| 3 | **上限常量写进代码**（抄 §9.5）：`MAX_COMMAND_BYTES` / `MAX_OUTPUT_BYTES` 等，配置只能下压 | 半天 | 配置读错不再意味着无上限 |
| 4 | **shell 分支的诚实披露**（抄 §9.7）：把「shell 只能操作 workspace 相对路径」写进工具描述 | 2 小时 | 模型不会写出注定被拦的命令，减少无效轮次 |
| 5 | **执行外置**（抄 §9.1）：`code_run` 改成可选的外部 runner（子进程 + 容器 / 独立服务），未配置时拒绝 | 架构级，独立阶段 | 平台进程里不再有用户代码，§8.4 的四条绕过路径全部消失 |

**建议的推进顺序**：先做 1–4（都是局部改动，不改变架构，能立刻降低风险），
第 5 条作为独立阶段（P1），和 Aivory 式的容器隔离是同一个决策点 ——
**要么把执行移出进程，要么把执行关进容器，两者选一，不能停在审计钩子**。

### 9.10 一条不能含糊的结论

LibreChat 的工程化程度体现在一个选择上：**它把绝大部分工程量放在「协议 + 校验 +
上限 + 审批」上，而不是放在「更聪明的审计钩子」上。**

审计钩子这条路的上限是明确的（§8.4 那几条绕过），继续加固的收益会越来越小；
而把执行边界前移到进程外，一次改动就能一次性解决整类问题。

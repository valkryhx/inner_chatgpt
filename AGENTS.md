# AGENTS.md — 本项目实施指南（后续 agent / 协作者从这里开始）

> 这份文件是**项目级交接文档**。目标：任何一个新 agent 只读本文件 + 它指向的几份文档，
> 就能在不问人的情况下继续推进。**开工前先读完本文件。**

---

## 0. 项目定位与铁律（不可违背）

**本项目 = 以 GenericAgent 为内核的 Web 多用户 Agent 服务。**

- 来源：当前代码是 `D:\git_codes\jiakuan_langchain` 的初始快照（曾被强制要求基于
  LangChain + LangGraph），现已决策**去除这两个库**，改为借鉴 `D:\git_codes\GenericAgent`
  的设计、在本项目重写自研内核。
- 形态：Web 服务（FastAPI + React），多用户、会话隔离、SQLite 持久化、MCP 工具接入、
  SSE 流式、上下文压缩。
- 目标：逐步把 GenericAgent 的成熟能力（代码执行 / 文件编辑 / subagent / team / task /
  计划模式 / goal / hooks）搬进 Web 多用户形态。浏览器控制暂不做。

铁律（不可违背）：

1. **只写真实可运行代码。** 不写伪代码、不写“示意性实现”。
2. **不引入 langchain / langgraph。** 新代码一律自研；需要参考时看 GenericAgent，不 import 它。
3. **借鉴设计，不 import GA 包。** GenericAgent 的 `handler.py`（约 13.6 万字符）、
   `app.py`（约 14.6 万字符）是 CLI 总装层，**不得整体搬入**；只借其循环/模型/压缩/子代理设计。
4. **契约稳定。** SSE 事件名与字段、HTTP API 路径与响应结构、前端契约不得破坏。
5. **每阶段可独立验证。** 允许新旧实现并存、用开关切换，验证通过再删旧代码。
6. **不丢历史数据。** 任何迁移前先备份 `backend/data/app.db`（含 `-wal` / `-shm`）。
7. **写文件一律 UTF-8 / LF**（无 BOM）。
8. **不覆盖前一阶段产物**：每阶段输出到自己的目录/文档。

### 0.1 当前阶段

**P0：去 LangChain / LangGraph 的轻量化基线替换 —— 已完成（2026-11-05）。**

- 后端已无任何 `langchain* / langgraph*` import；`requirements.txt` 已移除 5 个相关依赖，新增 `httpx`。
- 自研内核 4 件套：`llm_client.py`（httpx，chat/responses 双线流式）、`tools.py`（Tool/ToolRegistry）、
  `history_store.py`（`session_history` 单表历史）、`agent_loop.py`（显式工具循环 + 悬空 tool_call 自愈）。
- 历史数据已迁移：89 个会话从 langgraph checkpoint 解码写入 `session_history`（迁移前已备份 DB）。
- 真实 LLM E2E 通过；`backend/tests` 单测通过。

**P1：Skill 模块 + 基础工具（read / write / patch / runcode / load_skill） —— 已完成（2026-11-05）。**

已完成内容（详见 `docs/20261105-changelog-SkillP1.md`）：

- 内置工具（注册进同一 `ToolRegistry`，与 MCP 工具同构）：
  `file_read` / `file_write` / `file_patch` / `list_files` / `code_run` / `load_skill`。
- Skill 模块：文件系统 `SKILL.md`，双轨存储（`backend/skills_builtin/` 只读内置 +
  `backend/data/skills/<user_id>/` 私有）；**渐进披露**（system prompt 只放 L1 清单，
  正文由 `load_skill` 按需加载 L2）。
- 用户隔离：所有文件工具限制在 `backend/data/workspaces/<user_id>/`；路径 `resolve()` 后校验，拒绝越界。
- API：`/api/me/skills` CRUD + `/api/skills` 内置列表；前端「设置 → 我的 Skills」tab。
- 单测 23 项通过（原 12 + 新增 11），前端 `npm run build` 通过，真实 LLM E2E 验证
  `load_skill → file_write → code_run` 全链路。

**P2：Subagent（进程内 asyncio 子代理运行时）—— 已完成（2026-11-05）。**

已完成内容（详见 `docs/20261105-changelog-SubagentP2.md`）：

- 新包 `backend/app/subagent/`：`context`（AgentContext 多租户身份贯穿）/`store`（subagent_runs +
  subagent_messages 侧链表）/`events`（StopSignal + EventBuffer 单队列）/
  `capabilities`（闭世界工具裁剪）/`registry`（深度/槽位/重名/淘汰护栏）/
  `runtime`（子代理 Runtime + 写根隔离）/`host`（spawn/wait/read/list/close 生命周期）/
  `tools`（5 个编排工具）/`prompts`（root + child 提示词）/`session`（每轮派生 TurnRuntime）。
- 5 个编排工具只注入 root agent；子代理 registry 里**根本不存在它们**（prompt 不是安全边界）。
- 子代理用**独立侧链 history**，不写 `session_history`，避免主对话被中间产物污染。
- 子代理事件并入主对话同一条 SSE（`subagent_start` / `subagent_event` / `subagent_end`），
  前端新增可折叠 `SubagentCard`。
- 停止级联（主 stop → 所有子代理 stop + cancel）、`wait` 超时不杀子、墙钟 `SUBAGENT_TIMEOUT_S`、
  `wait_agent.since_event_seq` 增量事件。
- 单测 64 项通过（原 42 + 新增 22），前端 `npm run build` 通过，3 次真实 LLM E2E + Playwright UI 验证。

**P3（下一步）：** team / task 工具 → 计划模式 → goal → hooks。浏览器控制暂不做。
长出来的位置见 `docs/20261105-Subagent调研与设计方案.md` §14。
路线图见 `docs/20261105-去LangChain依赖-决策记录.md`。

### 0.2 必读文档（按顺序）

| 文档 | 用途 |
|---|---|
| `docs/20261105-changelog-SubagentP2.md` | **P2 已完成清单 + 自测（最新，先读这份）** |
| `docs/20261105-changelog-SkillP1.md` | **P1 已完成清单 + Skill/基础工具自测（先读这份）** |
| `docs/20261105-Skill与基础工具设计与实施.md` | P1 设计：存储/隔离/渐进披露/工具边界/测试 |
| `docs/20261105-changelog-P0.md` | P0 已完成清单 + 如何自测 |
| `docs/20261105-去LangChain依赖-决策记录.md` | 决策、边界、路线图 |
| `docs/20261105-去LangChain依赖-实施文档.md` | P0 可执行蓝本（模块设计/步骤/风险/测试） |
| `docs/20261105-LangChain-LangGraph依赖审计.md` | 旧代码审计（用在哪、如何替换） |
| `docs/20261105-前端审美提升设计.md` | **前端审美提升方案（Design System v2）**：现状诊断、暖纸+陶土配色 token、去气泡消息流、配色对比度实测、分批实施与验收清单 |
| `docs/20261105-changelog-前端批次1.md` | **前端批次 1 已完成**：Token 层替换（暖纸+陶土）、四档位回归通过、两个响应式缺陷修复 |
| `docs/20261105-changelog-前端批次2.md` | **前端批次 2 已完成**：文本去气泡（assistant 零容器 / user 保留输入容器）、工具改步骤行 StepGroup、思考默认折叠、`--measure` 宽度自适应 |
| `docs/20261105-changelog-侧栏身份区与设置弹窗.md` | **侧栏身份区重做 + 设置弹窗定高**：全名替字母头像、整行可点进设置、登出降级为次要动作；modal 固定高度，切 tab 不跳尺寸 |
| `docs/20261105-Skill目录包导入与公用技能设计与实施.md` | **Skill 目录包导入 + 公用技能层**：三层模型 builtin/shared/user、zip 安全解压（穿越/软链/炸弹防护）、`backend/skills_shared/` 公用目录、已导入 2 个 Codex 技能 |
| `docs/20261105-Subagent调研与设计方案.md` | **P2 设计文档（已实施）**：GA 进程树 vs Aivory 子任务 的调研对比、进程内 asyncio subagent 架构、`AgentContext` 多租户贯穿、5 个编排工具契约、停止/超时/失败语义、前端 subagent block、6 阶段实施蓝本与测试计划 |

### 0.3 目录约定（写文件前先确认放在哪个目录）

本项目四个顶层目录职责严格分开，**新建文件前先判断它属于哪一类，不要随手丢在根目录或 docs**。

| 目录 | 用途 | 放什么 / 不放什么 |
|---|---|---|
| `docs/` | **设计文档 + 进度记录**（长期留存、需要阅读） | 放：设计方案、实施文档、决策记录、changelog、进度记录、审计/调研报告。**只放 `.md`**。 |
| `misc/` | **零碎、不那么重要的内容** | 放：参考材料、截图、临时说明、外部粘贴文本（如 `*.txt`）、他人项目笔记。可读但与主线弱相关。 |
| `work/` | **临时测试产物的存放目录** | 放：一次性脚本（`_*.py`）、日志（`*.log`）、探针、构建监视脚本、临时 dump、调试快照。**可随时清理，不入正式交付。** |
| `backend/` `frontend/` | 产品代码 | 只放能跑的实现代码与资源；不要把测试产物、日志、截图塞进去。 |

**docs 目录写法与格式要求**（重要）：

1. **文件名必须是 `日期-名字`**（`YYYYMMDD-中文简述`），例如 `docs/20261105-Skill与基础工具设计与实施.md`。
   禁止无日期的裸名字（如 `design.md`）；英文调研/外部资料可保留原名，但也应尽量带日期前缀。
2. **统一用 Markdown**，UTF-8 无 BOM、LF 换行。
3. **新文档建议结构**：`# 标题` → `> 一句话摘要/日期/状态` → 背景与目标 → 设计与决策 → 实施步骤 → 测试与验证 → 风险与未决项 → 变更记录。
4. **两类文档分开写**：设计与决策写在「设计文档」，每阶段完成情况写在「changelog / 进度记录」，二者互相引用，不要混成一篇。
5. **不覆盖前一阶段产物**：进度记录按阶段新建（或追加到最新 changelog），不改写历史文档的既有结论。
6. docs 里**不放图片/二进制/临时文本**——截图等放 `misc/`，需要引用时写相对路径 `misc/xxx.png`。

**work 目录使用约定**：

- 一次性脚本用 `_` 前缀（如 `work/_patch.py`），跑完即删，避免堆积。
- 日志统一放 `work/`（如 `work/backend_hot.log`），不要写进仓库源码目录。
- `work/` 内容视为**可丢弃**：任何需要长期保留的结论，必须落到 `docs/` 的文档里，而不是留在 work。

## 1. 环境铁律（Windows：外层 PowerShell，内层 Git Bash）

### 1.0 三行速查（每次调用 `exec_command` 前必须过一遍）

1. 工具参数固定 `shell="powershell"`、`login=false`；**`shell` 永远不填 `bash`**。
2. `cmd` 外层只能是 PowerShell 语法：先 `$script = ` + @' 起始 here-string，再 `& 'D:\Git\bin\bash.exe' -lc $script`。
3. 一切 Bash 语法（`export` / `&&` / `|` / `cat <<` / `for`）只能写在 here-string 内部，它们只是 `-lc` 的 payload。

> 判据：**外层 `cmd` 是 PowerShell，内层才是 Git Bash。**
> 一旦发现自己在想把 `bash` 填进 `shell`，或把 `&&` / `|` / `cat <<` 直接写进 `cmd` —— 停手，方向错了。

### 1.0.1 可复制模板（推荐多行写法，照抄不要改写）

```powershell
$script = @'
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
cd /d/git_codes/inner_chatgpt
# ... bash payload ...
'@
& 'D:\Git\bin\bash.exe' -lc $script
```

- 第一行 `$script = @'` 与结束行 `'@` 必须**各自独占一行**；`'@` 必须是行首字符，其后不接任何内容。
- `D:\Git\bin\bash.exe` 是本机实测路径；换机器先 `where.exe bash` 确认。

### 1.0.2 报错 → 诊断（出现下表现象，说明外层不是 PowerShell）

| 现象 | 真实原因 | 正确做法 |
|---|---|---|
| `/usr/bin/bash: line 1: =: command not found` | 整段 PowerShell 被当 bash 执行，外层是 bash/WSL | 改回 `shell="powershell"`，Bash 内容放进 here-string |
| `syntax error near unexpected token '&'`，且回显含 `& 'D:\Git\bin\bash.exe'` | 同上，调用行被 bash 解析 | 同上 |
| 自己把 `shell` 写成了 `bash` | 本机命中 WSL 存根，必错 | 改成 `shell="powershell"` |

> 注：输出里出现 `bash=/usr/bin/bash`、`MINGW64_NT-...` 是**期望结果**（子进程已是 Git Bash），不要误判成“外层成了 bash”。


### 1.0.3 写「含 PowerShell here-string 示例」的文档时

- here-string 的结束是**任意行首出现结束符两字符**即触发，哪怕它只是正文示例的一部分。
- 所以：**不要**在 payload 里直接写出“行首的结束符”；改用占位符，最后在 Python 里用 `replace` 将占位符还原成真实结束符。同理也不要把开头符写在行首示例里。
- 通用做法：写这类文档用独立 `.py` 脚本（raw 字符串 + 占位符替换），不用 heredoc 直接落盘。


- **命令内容在 Git Bash 子进程执行，但工具外层永远不是 Bash。** `exec_command` 的默认 shell 是 **PowerShell**；**禁止使用 `shell="bash"`**，本机该值可能命中 WSL 存根，并且会把下面的 PowerShell wrapper 当成 Bash 语法而报错。唯一允许的调用骨架是：

  ```text
  exec_command(
    cmd="& '<Git Bash 可执行文件绝对路径>' -lc '<bash-payload>'",
    shell="powershell",
    login=false,
    workdir=...
  )
  ```

  多行 payload 用 PowerShell 单引号 here-string 传入：

  ```text
  $script = @'
  ... bash payload ...
  '@
  & '<Git Bash 可执行文件绝对路径>' -lc $script
  ```

  `$script = @'`、`'@` 和开头的 `&` 都是 **PowerShell 语法**，不能作为 `shell="bash"` 的 `cmd`。看到 `export`、heredoc、`bash $'\r'`、`&&`、管道等 Bash 语法时，也只说明它们是 `-lc` 的内部 payload，绝不代表应把工具参数改成 `shell="bash"`。

  - ❗ 不显式传参时走的是 **PowerShell**（`PSVersion=...`）——这就是之前“嘴上说 Git Bash、实际跑 PowerShell”、以致中文与 heredoc 报错的根因。**不要依赖默认值，也不要只“声称”在 Git Bash。**
  - ⚠️ **本机实测（2026-10-19）**：`shell="bash"` 会命中 **WSL 存根**，不是 Git Bash；即使某次没有命中 WSL，也会把 PowerShell wrapper 当成 Bash 语法并报 `syntax error near unexpected token '&'`。两种情况都不得改用 `shell="bash"`。
  - 🛡 Git Bash 绝对路径兜底：先用 `where.exe bash` / `where.exe git` 确认本机安装位置，再用 PowerShell 包装器调用，最确定：

    ```powershell
    & '<Git Bash 可执行文件绝对路径>' -lc '<script>'
    ```

  - 📍 **Git Bash 路径是机器相关的**：本机实测 `D:\Git\bin\bash.exe`，可直接用于 §1.0.1 模板；**换机器先 `where.exe bash` / `where.exe git` 确认**，其他机器用 `where.exe bash` 自行探测确认。gitconfig / node 路径同理以本机 `where.exe` 结果为准。

  - ✅ 判定是否真的进入 Git Bash：输出应包含 `bash=/usr/bin/bash`、`MINGW64_NT-...`、`LANG=C.UTF-8`；不要只看命令是否返回 0。
- 路径映射：盘符 `X:\x` → `/x/x`（如 `D:\x` → `/d/x`）。
- **路径格式不可混用**：Git Bash 内用 POSIX 路径（`<盘符>/...` 或相对路径）；Windows 程序（`python` / `node`）**不认** POSIX 路径，要传原生路径（`X:\...` / `X:/...`）。同一条命令里不要混用两种。
- Python 用 **`python`**（不是 `python3`，后者命中 WindowsApps 存根）。
- Node 用 `node`（本机实测 v25.2.1；路径与版本因机器而异，以 `where.exe node` / `node --version` 为准）。
- Git Bash 的 `find -printf` 可能不可用；没有 `strings` 命令（用 `grep -a`）。
- 写含 `$` 的文件时，用**单引号** here-string（`@'...'@`），否则 `$` 会被 PowerShell 展开。
- **git 行尾**：系统级 gitconfig（`git config --list --show-origin` 可查位置）里 `core.autocrlf=true`（Git for Windows 安装器默认），会让 `git add` 刷 LF/CRLF 警告、`git status` 出幽灵 diff。**本仓库已用仓库内 `.gitattributes`（`* text=auto eol=lf`）统一为 LF 根治**——只影响本仓库，不动系统级 `core.autocrlf=true`。**遇到行尾/CRLF 警告不要再排查，`.gitattributes` 已统一解决。**
- **写文件一律 LF**：编辑/生成任何文本文件都直接写成 LF（`\n`），不要 CRLF。

  - 仓库已用 `.gitattributes`（`* text=auto eol=lf`）在入库时自动规范化。**看到"整文件 diff"先怀疑行尾，一步确认即可，
    不要展开排查、不要反复 checkout/重写** —— 那是纯 token 浪费。
  - 程序化写文件的坑（本机实测）：**Python `open(p,"w")` 在 Windows 会把 `\n` 翻成 `\r\n`**
    → 必须 `open(p,"w",newline="\n")`（或写二进制）；Node `fs.writeFileSync` 默认原样写，确保字符串里是 `\n`。
  - 快速校验（期望 0）：`python -c "import sys; print(open(sys.argv[1], 'rb').read().count(b'\r'))" '<file>'`
- **md / 文档命名**：新建 markdown 及文档时，文件名一律用 `日期-名字` 格式（日期 `YYYYMMDD`），例如 `docs/20261012-研究workflow.md`；不要用无日期的裸名字。

- **长文本写入必须分段（Windows / Codex 实测）**：
  - Windows `CreateProcessW` 的命令行硬上限是 **32767 UTF-16 code units**；本机 `bash -c` 脚本体实测最大 **32735**，32736 报 `WinError 206`。
  - 但 Codex tool-call 参数会在远低于 OS 上限时出现截断；本项目实际出现过约 3.1 万字符 / 4 万 UTF-8 字节的 heredoc 只写入前半段，末尾 `EOF` 未到达。
  - 因而禁止把整篇长文塞进一次 `exec_command`。**中文正文每段约 2K-4K 字符，ASCII/代码每段不超过 8KB；单条命令目标总长 ≤ 8KB，绝不超过 12KB。**
  - 长文第一段用 `cat > file <<'MDOC_20261021_A7F3'`，后续段用 `cat >> file <<'MDOC_20261021_A7F3'`；每次都使用**本次唯一的 delimiter**，并在正文结束后用完全相同的单独一行闭合。
  - **禁止在包裹长文时使用裸 `EOF`。** 引号 heredoc（`<<'EOF'`）只阻止变量展开，不能阻止正文里单独一行的 `EOF` 提前结束 heredoc；写“包含 heredoc 示例的 Markdown”时，外层 delimiter 必须不同于所有内层示例 delimiter。
  - 按 Markdown 章节边界拆，不拆 code fence、JSON object、表格行或源码片段的中间；每段独立闭合 heredoc。
  - 看到 `here-document ... delimited by end-of-file (wanted ...)` 时，先检查是命令截断还是正文中出现了同名的 delimiter 行：检查文件尾部与 delimiter 出现位置，从最后完整章节继续，换一个新的唯一 delimiter，并缩小下一段；禁止原样重试同一大命令。
  - 上述限制均为本机实测结论，直接照做即可。

- **heredoc 只用于「写死内容」，不用于「跑逻辑」（2026-10-26 补充）**：
  - 判定标准只有一条：**这段内容是不是原样落盘**。是 → 可以用 heredoc；需要循环、正则、读现有文件、条件分支、多步改动 → **一律写成独立 `.py` / `.mjs` 脚本再执行**，禁止用 `python - <<'X'` 跑脚本。
  - ❌ 反面例（本仓库实际踩过）：`python - <<'PY_X' && echo done` —— 正文和闭合行都没写，bash 报 `here-document ... delimited by end-of-file`，`python -` 读到空 stdin 进入 REPL，后面的 `&& echo` 照样执行，于是**既没改文件也没报错**。
  - ✅ 正面例（插入表格行 / 改行 / 条件写入这类“要判断”的活）：

    ```bash
    cat > work/_patch.py <<'PY_PATCH_20261026_0713'
    import sys
    path = sys.argv[1]
    text = open(path, encoding="utf-8", newline="").read()
    if "已插入" in text:
        print("skip: already patched")
        sys.exit(0)
    ...  # 真实逻辑
    PY_PATCH_20261026_0713
    python work/_patch.py AGENTS.md
    ```

  - 闭合行必须**独占一行且后面不接 `&&` / `;` / `|`**；命令里出现第二个命令时，把 heredoc 段写成单独一条 `exec_command`。
  - **heredoc 正文里不要出现“单独一行的 delimiter 候选”**，尤其别写 `EOF`、`END`、`DONE`、`PY`、`PYTHON` 这类通用词；统一用 `用途_日期_随机4位`，如 `PY_PATCH_20261026_K3M9`。
  - 每条一次写入的正文 ≤ 2K-4K 中文字符 / ≤ 8KB ASCII；超了就按 Markdown 章节或函数边界拆成多条 `cat >>`，每条独立闭合。
  - 需要写“包含 heredoc 示例的 Markdown”时，**外层 delimiter 必须与正文里所有内层 delimiter 不同**，且外层不要用 `MD`、`EOF` 这类会被示例正文命中的词。
  - **自查（写完必跑一条）**：`tail -c 200 <file>` 看结尾是否完整；`grep -c $'\r' <file>` 期望 0；含中文再 `python -c "open('<file>','rb').read().decode('utf-8')"`。
  - **写坏后的恢复顺序**（不要原样重试同一条命令）：① 看 `tail -c 300 <file>` 判断是“截断”还是“正文里撞了 delimiter”；② 若是截断，从最后一个完整章节/函数边界继续，换新 delimiter、缩小下一段；③ 若是撞 delimiter，只重写被截断的那一段；④ 逻辑类命令直接删掉 heredoc，改独立脚本重做。


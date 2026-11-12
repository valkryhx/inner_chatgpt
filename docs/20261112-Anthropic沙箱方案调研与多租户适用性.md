# Anthropic Agent Runtime / Sandbox Runtime 调研：能否当多租户沙箱用

> 日期：2026-11-12　状态：调研结论（未改代码）
> 触发：听人说「Anthropic agent runtime 是非常轻量化的容器，适合给多租户平台配置沙箱」，
> 需要确认这个说法是否成立，以及对本项目（Hlaudz，Windows + 多用户）有没有用。

## 0. 结论先说

**这个说法把三个不同的东西混成了一个，而且最关键的那句是错的。**

| 传闻中的说法 | 事实 |
|---|---|
| 有个东西叫 "Anthropic agent runtime" | **没有这个产品名**。相近的有三个：`sandbox-runtime`(srt)、Claude Managed Agents、Agent SDK |
| 它是「轻量化的容器」 | **srt 明确「不用容器」**（without requiring a container）。它的卖点恰恰是不要容器 |
| 适合给多租户平台配沙箱 | **srt 官方定位是本地开发机防护，第三方评测明确写 "Not a hosted or multi-tenant service"**、建议配 VM 跑不可信多租户代码 |

真正适合多租户的是另外两个：**Claude Managed Agents**（托管，Anthropic 侧跑沙箱）和
**Agent SDK + 你自己上 Docker/gVisor/Firecracker**（自托管）。

对本项目（Hlaudz）的具体建议见 §5。

---

## 1. 先把三个名字分开

这三者常被混为一谈，但定位完全不同。

### 1.1 `sandbox-runtime`（srt）—— 轻量，但不是容器

- 仓库：`anthropics/sandbox-runtime`，npm 包 `@anthropic-ai/sandbox-runtime`，
  命令 `srt`，Apache-2.0，2026-10-24 起开源（v0.0.81）。
- **官方第一句话就是：A lightweight sandboxing tool for enforcing filesystem and
  network restrictions on arbitrary processes at the OS level, without requiring a container.**
- 实现方式：用各平台**已有的 OS 原语**，不启动虚拟机、不拉镜像：
  - macOS：`sandbox-exec` + 动态生成 Seatbelt profile
  - Linux：`bubblewrap`，并把网络 namespace 整个移除
  - Windows：**alpha**，跑在专用本地账户 `srt-sandbox` 下 + WFP 出网围栏
- 定位：Claude Code 内部那个「沙箱化 bash 工具」的底座，开源给生态用。
  官方自标 **Beta Research Preview**，配置格式可能变。

所以「轻量化」这点是真的，但**轻量的原因是它不做容器**，不是「容器做得很轻」。

### 1.2 Claude Managed Agents —— 托管的 agent runtime

- 2026-04-08 公开 beta。**这个才是"hosted agent runtime"**。
- 四个概念：Agent（模型+提示词+工具+MCP+skills）、Environment（容器模板）、
  Session（跑起来的实例）、Events（SSE 事件流）。
- 沙箱由 Anthropic 管（或 self-hosted sandbox，但 harness 仍在他们侧）。
- 计费：标准 token 费 + **每运行 session-小时 $0.08**。
- 明确**不是**多模型编排器，只能跑 Claude；且当前不支持 ZDR / HIPAA。

### 1.3 Agent SDK —— 跑在你自己进程里的 harness

- 每次 `query()` 起一个 `claude` CLI 子进程，一个 session 一个子进程。
- 官方 hosting 文档里写得很直白：**1 GiB RAM / 5 GiB disk / 1 CPU 每 agent 是合理起点**，
  内存还会随 session 变长而涨。
- 多租户隔离要你自己做：`settingSources: []`、`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`、
  每租户独立 `CLAUDE_CONFIG_DIR` 与 `cwd`、代理侧配每租户 egress 规则。
- **这就是"1 GiB 一个 agent"的东西——离"轻量容器"最远。**

如果有人把 Agent SDK 的 runtime 说成「轻量化容器」，那是把 1 GiB 说成了轻量。

---

## 2. srt 的真实能力（客观版）

值得肯定的部分：

| 能力 | 说明 |
|---|---|
| 零冷启动 | 没有 VM/镜像，包一层进程就完事 |
| 双隔离模型 | 文件 + 网络都必须有，缺一个就能越狱 |
| 网络默认全禁 | 走 host 上的 HTTP/SOCKS5 代理，按域名白名单放行 |
| 文件默认只读允许、写全禁 | 写必须显式 allow；`denyWrite` 优先于 `allowWrite` |
| 拦截子进程 | 限制作用于**整个进程树** |
| 可审计 | 开源，边界可查 |
| 违反可观测 | macOS 可读系统 sandbox 违规日志；库提供 `getViolationsForCommand` |

### 2.1 官方自己写的 Windows 限制（与你直接相关）

你的环境是 Windows，这一节尤其重要。srt 的 Windows 支持是 **alpha**，README 列了这些：

- **沙箱账户是「每机器一个」，不是每用户一个。** 一旦给某用户的 profile 授了读权限，
  机器上**任何其他本地用户**启动的沙箱命令都能读到那些目录。README 明说：
  多用户共用的电脑上**不要**施加该授权（用 `--skip-profile-grant`）。
- **SMB 与 WSL 流量不被 WFP 围栏覆盖**（走内核 redirector / 独立服务，
  不一定带 `srt-sandbox` SID）。
- **可以主动绕过**：沙箱内进程可 `CreateRestrictedToken` 派生一个 deny-only SID 的 token，
  WFP BLOCK 规则就不匹配了，连接完全不被围栏。README 如实写明「no change to the filter
  descriptors closes it」，只能靠把策略反过来（默认 BLOCK）才能堵，而那会影响全机账户。
- 基于 Schannel 的客户端（系统 `curl.exe`、.NET、PowerShell web 请求）在沙箱内**不能用**。
- `proxyAuthToken` 会出现在 runner 命令行里，同机同 session 的其他 principal 可读。
- `--control-fd` 运行时更新配置时，**Windows 上文件访问集合改不了**（ACL 在启动时已按
  session 范围施加），只有网络列表能变。

**一句话**：srt 的 Windows 实现是 alpha，且它的安全模型假设「单用户开发机」。

### 2.2 第三方评测的结论

`agentsandboxes.org`（2026-11-08 更新）的评价很直接：

> Excellent lightweight guardrail for local agents; **pair with a VM for untrusted
> multi-tenant code.**
> Trade-offs: OS-level boundary, **weaker than a VM**; Beta; **Not a hosted or
> multi-tenant service.**

---

## 3. 为什么「OS 原语沙箱」本身不适合当多租户边界

这不是 srt 的缺陷，是这一类方案（含 bubblewrap / Seatbelt / 同机独立账户）的共性：

1. **边界是同机的。** 多租户要求「租户 A 拿不到租户 B 的任何东西」，
   而同一台机器上共享内核、共享文件系统、共享网络栈。任何内核漏洞或共享资源
   （/tmp、共享内存、进程表）都是横跨租户的通道。
2. **srt 的威胁模型不含「主动越狱」。** 它自己也写：定位是「约束正常行为的 agent」，
   不是「防住一个专门写来突破沙箱的对手」。多租户里租户天然是有动机的对手。
3. **账户是一台机器一份。** 上面 §2.1 已说明，Windows 上这点直接破坏租户隔离。
4. **Anthropic 自己怎么做的？** 值得看他们的真实选择：
   - claude.ai 的代码执行：**gVisor 容器**，跑在隔离的服务器端基础设施上，
     「保护我们自己的基础设施和租户彼此」——他们说的是 **tenant from one another**。
   - Claude Cowork：**完整虚拟机**（macOS 用 Virtualization.framework，Windows 用 HCS），
     有自己的内核、文件系统、进程表。
   - Claude Code：srt（OS 原语）——**因为用户是开发者，在自己机器上，人机协同**。

   **规律很清楚**：越不可信、越需要无人值守，隔离层级越高。
   多租户是这条轴的终点，对应的是 gVisor/微虚拟机，不是 OS 原语。

---

## 4. 那 srt 在什么场景真的有用

不是没用，只是用错地方会出事：

- ✅ 给本地 agent / CLI 加一层护栏（写限制在工作区、网络默认禁）
- ✅ **给本地 MCP server 加护栏**——在 `.mcp.json` 的启动命令前加 `srt` 就行，
  这对本项目有现实价值（见 §5.2）
- ✅ 想减少权限弹窗又不想上容器（Anthropic 报的收益是权限提示减少 84%）
- ❌ 当多租户之间的唯一隔离边界

---

## 5. 对本项目（Hlaudz）的具体评估

先说清现状：本项目的 `code_run` 隔离是**进程内的写入边界**，不是沙箱。
`backend/app/run_guard.py` 自己的注释就写着：

> 这是进程内的防御层，不是操作系统级沙箱……真正的生产级隔离仍应由容器 /
> 独立账户 / OS sandbox 提供。

即：**Hlaudz 目前对租户之间没有 OS 级隔离**。这是已知的、写在代码里的债。

### 5.1 如果你的部署是「单机、少量可信用户」

那 srt 可能够用，但**不要指望它解决多租户**。它能挡住「模型误写 `~/.ssh`」
这类意外，挡不住租户主动越狱。

### 5.2 一个立刻有价值的小用途：给 MCP server 上护栏

这是 srt 最贴合本项目的场景，成本极低：

```json
{
  "mcpServers": {
    "some-third-party": {
      "command": "srt",
      "args": ["npx", "-y", "@modelcontextprotocol/server-filesystem"]
    }
  }
}
```

第三方的 MCP server 代码你不审计，但它现在能读整个 home。用 srt 包一层，
就把它限制在你给的目录和域名里。**这个不涉及多租户，纯收益。**

⚠️ 但注意：**Windows 上是 alpha**，上面 §2.1 的坑（SMB/WSL 不走围栏、可派生 token 绕过、
Schannel 客户端不可用）都会遇到。要用建议先在 Linux 上验证，或接受 alpha 的风险。

### 5.3 如果真要上多租户不可信代码

按隔离强度从低到高，我的建议排序：

| 方案 | 冷启动 | 隔离强度 | 适合 |
|---|---|---|---|
| 现状（进程内写边界） | 0 | 无 OS 边界 | 单用户/全可信 |
| **srt** | 进程启动 | OS 原语，同机共享内核 | 本地护栏，非租户边界 |
| Docker + seccomp/AppArmor | 秒级 | 容器，需加固 | 中等可信、可控镜像 |
| **gVisor** | 秒级 | 用户态内核，系统调用面大幅收窄 | **多租户代码执行的现实选择** |
| 微虚拟机（Firecracker / libkrun） | <100ms–数百 ms | 独立内核 | 强隔离且要密度 |
| 每租户独立 VM | 数秒–数十秒 | 最强 | 合规/高价值 |

**Anthropic 自己在 claude.ai 上选的就是 gVisor**（§3.4）。这条路有先例、有运维经验可循。

关键前提（不管选哪个）：**凭证绝不能进沙箱**。Anthropic 的工程文章把这个当作结构性修复，
而不是靠"缩小 token 权限"——因为「Claude 越来越聪明，有限 token 能干什么越来越难假设」。
对本项目意味着：用户自己的 LLM API key 不应该和不可信执行代码待在同一空间里。

---

## 6. 如果你只是想要「轻量」

那 srt 确实轻，但先问一句：**你要的是「轻」还是「隔离」？** 这两个在多租户下是矛盾的。

- 要轻 → srt 名副其实，但它给的是**护栏**，不是**边界**。
- 要隔离 → 隔离本身有成本，gVisor/微虚拟机已经是「轻量」里最强的档，
  再往下就是拿安全换性能。

「轻量化的容器 + 适合多租户」这句话之所以吸引人，就是因为它承诺两者兼得。
而 Anthropic 自己的产品线恰好证明：**他们没在同一个产品里同时给你这两样**——
claude.ai 用 gVisor（隔离优先），Claude Code 用 srt（轻量优先），
Cowork 用 VM（用户不会 bash，必须绝对边界）。

---

## 7. 一句话总结

- 没有叫 "Agent Runtime" 的 Anthropic 产品；
- `sandbox-runtime`(srt) 是真的、真轻量，但**明确不用容器**，且**官方不建议**当多租户边界；
- 想给 Hlaudz 配多租户沙箱，**srt 是错的选择**，该看 gVisor / 微虚拟机；
- 但 srt 包 MCP server 这件事**现在就能做**，是纯收益（Windows 上注意 alpha 限制）。

## 8. 资料来源

- `github.com/anthropics/sandbox-runtime`（README、Windows alpha 与 Known limitations、v0.0.81 release notes）
- `anthropic.com/research/how-we-contain-claude`（三种隔离模式：gVisor 容器 / srt / 完整 VM）
- `anthropic.com/research/managed-agents`（Managed Agents 的接口拆分与凭证不入沙箱）
- `code.claude.com/docs/en/agent-sdk/hosting`（子进程模型、1 GiB/agent、多租户隔离清单）
- `platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes(-security)`（自托管沙箱的责任边界）
- `agentsandboxes.org/sandboxes/anthropic-srt/`（第三方评测：明确写非多租户服务）

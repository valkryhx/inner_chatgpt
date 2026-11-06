# Codex 修复须知：Web 自测 Bug 与修复建议（2026-11-06）

> 对象：接手修复的 Codex / 协作者
> 来源：`docs/20261106-Web自测Bug清单.md`（完整证据与复验记录）
> 环境：后端 `uvicorn app.main:app --reload`（127.0.0.1:8000）；前端 `frontend/dist`（最近构建 2026-11-06 18:30:04）；真实模型仅使用「湛卢」`moma/deepseek-latest-flash`（openai_chat）。
> **测试期间存在并发改码/重建 dist**：本文所有行号与结论对应 2026-11-06 18:53 的代码；若你已在同一文件上继续编辑，请以当前文件为准，不要机械套用行号。

---

## 0. 先看这里：两个 P0 必须先修

1. **BUG-01：普通子代理 100% 启动失败**（`spawn_agent` 全链路不可用）。
2. **BUG-02：客户端断开/刷新/关标签即泄漏 Team 配额**（用户此后永远 `team_unavailable`，需重启后端）。

这两项修完后，再处理 BUG-03（可见性）与其余 P2/P3。**不要先改 UI 文案而跳过 P0。**

---

## 1. 修复顺序、验收门槛与红线

### 1.1 建议顺序

| 顺序 | Bug | 预估改动 | 风险 |
|---|---|---|---|
| 1 | BUG-01 子代理角色 | 1–2 行 + 防御 | 低 |
| 2 | BUG-02 配额泄漏 | `chat.py` 清理段 + `TeamHost` 同步释放 | 中（涉及取消语义） |
| 3 | BUG-03 失败可见性 | 前端 `mergeTeamEvent` + 面板提示 | 低 |
| 4 | BUG-05 / BUG-12 错误与校验 | 前端提示、PATCH 校验 | 低 |
| 5 | BUG-06 压缩阈值 | `compaction.py` 策略 | 中 |
| 6 | BUG-04 / BUG-07 / BUG-08 / BUG-09 / BUG-10 / BUG-11 / BUG-13 / BUG-14 | UI/文案/样式 | 低 |

### 1.2 红线（不要破坏契约）

- 不引入 `langchain` / `langgraph`；沿用自研内核。
- 不改 SSE 事件名与字段、HTTP API 路径与响应结构、前端契约。
- 不改 `llm.yaml`、不把真实 key 写进仓库、不动其他用户数据。
- 所有文件 UTF-8 / LF（无 BOM）。
- 每修一项都要能独立验证；不要顺手大重构。
- 修完必须跑：
  ```bash
  cd backend && python -m pytest tests -q        # 基线：127 passed
  cd frontend && npm run build                    # 后端单端口托管 dist
  ```

### 1.3 回归/复验脚本（仓库根目录执行）

```bash
python work/claude_test_and_review/_repro_subagent_role.py   # BUG-01：期望 teammate / OK
python work/claude_test_and_review/_verify_bug02.py           # BUG-02：期望 team_start（用全新用户或先重启后端）
python work/claude_test_and_review/_suite_api.py              # 基础 API：期望 0 FAIL
python work/claude_test_and_review/_suite_api.py chat stop subagent team
```

> 注意：`_verify_bug02.py` / `_test_lease*.py` 会故意触发泄漏；**每跑一次都会污染该用户的进程内配额**，建议每次用新用户（claude5/6/7…）或重启后端后再跑。当前 `claude2/5/6` 已处于泄漏态。

---

## 2. 逐项修复单

### BUG-01（P0）普通子代理 100% 启动失败

**现象**：任何 `spawn_agent` 都在第 0 轮失败，子代理卡片显示「通用 · 失败」，`turns=0`；`spawn_teammate` 正常。

**证据**：会话 `subagent-test` 回复原文含
`ValueError: build_teammate_tools requires a teammate TeamHost`；
`shots/15-subagent-error.png`；`runs/api_20261106_175552/subagent_frames.json`。

**最小复现**：`_repro_subagent_role.py` 输出：

```text
parent role: lead team: team_x
child role:  lead team: team_x node.role: general   # ← 应为 teammate
from_agent role: lead
build_teammate_tools FAILS: ValueError build_teammate_tools requires a teammate TeamHost
```

**根因链**：

1. `backend/app/subagent/registry.py:133`（`create_child`）：
   ```python
   ctx = parent_ctx.child(name, team_id=team_id,
                          role="teammate" if team_id else parent_ctx.role)
   ```
   当 `team_id` 参数为空、但父上下文已带 team 时，传入的是 `parent_ctx.role`（`lead`）。
2. `backend/app/subagent/context.py:53-56`（`child`）：
   ```python
   team_id=team_id or self.team_id,
   role=role if (team_id or self.team_id) else self.role,
   ```
   因为 `self.team_id` 非空，子上下文 `role` 被设成 `lead`。
3. `backend/app/subagent/runtime.py:93-96`：
   ```python
   if self.ctx.team_id:
       team_host = TeamHost(TeamScope.from_agent(self.ctx.scope()))  # role=lead
       for tool in build_teammate_tools(team_host):                 # 要求 teammate
   ```
   `team_tools.py:207-211` 的 `build_teammate_tools` 直接抛 `ValueError`。

**建议修法（按优先级）**：

1. 最小修复（`registry.py:133`）：子代理在 Team 中永远是 teammate：
   ```python
   ctx = parent_ctx.child(name, team_id=team_id, role="teammate")
   ```
   （无线程时 `context.child` 的 `else self.role` 分支仍会继承父角色，不破坏非 Team 场景。）
2. 防御修复（`subagent/runtime.py:93-96`）：显式构造 teammate scope，不依赖 `from_agent` 推断：
   ```python
   team_scope = TeamScope(
       user_id=self.ctx.user_id, session_id=self.ctx.session_id,
       generation_id=self.ctx.generation_id, team_id=self.ctx.team_id,
       agent_id=self.ctx.agent_id, role="teammate",
   )
   team_host = TeamHost(team_scope)
   ```
3. 可选：`build_teammate_tools` 前校验 `host.role == "teammate"`，或让 `from_agent` 对 `agent_id.startswith("sub:")` 一律映射为 `teammate`。

**验收**：

- `_repro_subagent_role.py`：`child role: teammate`，`build_teammate_tools: OK`。
- 真实 LLM prompt（`_suite_api.py subagent`）：`subagent_start`/`subagent_end` 均出现，`subagent_end.status=completed`，结果含 `391`；`GET /sessions/{id}` 的 `blocks` 中有 `subagent` 块。
- 确认 teammate 路径未回归（`_suite_api.py team`：任务 completed、144/169）。

**注意**：`AgentContext.child` 的 `role` 语义同时被 Team 和普通子代理使用，改 `context.py` 时要确认 `team_id` 为空时仍继承父角色；建议优先改调用点（`registry.py`）。

---

### BUG-02（P0）客户端断开泄漏 Team 配额（用户级永久）

**现象**：生成中浏览器断连（关标签/刷新/`AbortController.abort()`，前端的「停止」按钮第一步就是 abort）后，该用户之后每次生成都收到 `team_unavailable`；同一用户所有会话都受影响；**重启后端进程才恢复**。正常完成后释放正常；仅调 `/stop` 不 abort 时也正常。

**证据**：

- `_test_lease2.py`（abort + `/stop`）→ 下一轮 `['team_unavailable']`。
- `_test_lease3.py`（全新用户 claude3：仅 abort）→ A1/A2（6s/21s 后）均 `team_unavailable`。
- `_verify_bug02.py`（全新用户 claude4：仅 abort）→ `team_unavailable`。
- 复验（18:53）：claude5（仅 abort）、claude6（abort+stop）→ 下一轮均 `team_unavailable`；对照 claude7 正常回合 `team_start`（证明按用户泄漏，不是全局故障）。

**根因**：

`backend/app/routers/chat.py:352-374`（`_event_stream` 的 `finally`）：

```python
finally:
    if turn is not None:
        try:
            await turn.host.close_all()          # ← 取消范围内第一个 await
        except Exception:
            pass
        ...
        try:
            await turn.team.set_status(...)
        except Exception:
            pass
        try:
            await turn.team.release_lifecycle()  # ← 被跳过
        except Exception:
            pass
```

客户端断连时 Starlette/anyio 的取消范围会在 `await` 处再次抛 `asyncio.CancelledError`；它继承 `BaseException`，**不会被 `except Exception` 捕获**。只要 `close_all()`（内部有 `await asyncio.sleep`）被取消打断，后面的 `set_status` / `release_lifecycle` 全部跳过。`QuotaManager` 的用户级 semaphore 永久 -1（`quota.py` 的 `_user_teams`，默认 `USER_MAX_ACTIVE_TEAMS=1`）。

**同一路径的次生影响（务必一起修）**：

- `turn.host.close_all()` 未执行 → 子代理任务可能继续在后台跑、持有 subagent/code_run 配额；`hub._subagent_hosts` 不 pop（`runtime.py`），内存泄漏。
- `team_store` 的 generation/team 行是否收敛取决于后续 GET/发送；本次观测过 generation+team 双 `stale`（会话 `co_b3d7a66462cb`），面板显示「已过期」。
- 清理段全部 `except Exception: pass`，线上无日志，只能靠用户「Team 不出现了」发现。

**建议修法**：

1. 在 `TeamHost` 增加**同步**释放方法（`release_lifecycle` 本身没有 await，但为了语义明确）：
   ```python
   # backend/app/team_host.py
   def release_lifecycle_now(self) -> None:
       self._require_lead()
       lease, self._lifecycle_lease = self._lifecycle_lease, None
       if lease is not None:
           lease.release()
   ```
   保留原 `async def release_lifecycle` 作为兼容包装（内部调用 `release_lifecycle_now`）。
2. `chat.py` 的 `finally` **第一行、任何 await 之前**同步释放；其余每个 await 都吞 `BaseException`，并在最后重新抛出原异常（需要先 `import logging; logger = logging.getLogger(__name__)`）：
   ```python
   finally:
       if turn is not None:
           try:
               if turn.team is not None:
                   turn.team.release_lifecycle_now()   # 同步、不可被取消
           except BaseException:
               logger.warning("release team lease failed", exc_info=True)
           try:
               await turn.host.close_all()
           except BaseException:
               logger.warning("close subagent host failed", exc_info=True)
           ...  # 其余清理同样用 except BaseException + 日志
   ```
   （示例逻辑：清理过程吞掉 `CancelledError` 以保证后续步骤执行；如需保持取消语义，可在清理完成后把原始异常重新 `raise`。）
3. **更稳的做法（推荐一起做）**：把「活跃 Team lease / SubagentHost」登记到进程内 registry，键 `(user_id, session_id, generation_id)`：
   - `/stop` 端点（`routers/chat.py::stop_generation`）在 set event + cancel task 后，直接从 registry 同步释放该 lease、`await host.stop_all()` 并 `unregister_subagent_host`。
   - `guarded_stream` 的 `finally` 再做一次幂等兜底。
   - registry 中过期项按 generation 清理，避免内存增长。
4. 可观测性：`prepare_turn_team` 捕获异常时 `logger.warning(..., exc_info=True)`；`_event_stream` 清理段不要静默 `pass`。

**验收**：

- 干净进程下 `_verify_bug02.py`：下一轮 `team_start`（当前是 `team_unavailable`）。
- 干净进程下 `_test_lease3.py`：A 段（仅 abort）与 B 段（abort+stop）之后，新会话都能 `team_start`。
- 断连后同会话再次发送不返回 409；`GET /team` 状态为 `stopped`/`completed`，不是 stale/active。
- 断连后的子代理：`hub.list_*` 无残留（可加日志/内部断言），无后台 LLM 继续消耗。
- 正常完成、仅 `/stop`、并发 409 三条旧路径不回归（`_suite_api.py stop concurrency`）。

**风险提示**：

- 不要在 `finally` 里 `await` 一个可能被取消的清理任务后再释放；顺序必须是「同步释放 → 其余清理」。
- 若使用 `asyncio.shield`，注意 anyio cancel scope 的取消是「级别触发」的，shield 不一定能救；同步释放最可靠。
- 修复后请同步更新 `docs/20261106-Agent停止卡死与陈旧租约自愈.md` 的停止语义说明（如有必要）。

---

### BUG-03（P1）`team_unavailable` 被前端静默丢弃

**现象**：后端在 Team 创建失败时发 `team_event {type:"team_unavailable", payload:{error}}`；前端只显示「未启动 / 发送消息后…」，无任何错误提示。

**证据**：复验时 claude2（已泄漏）发送消息，Team 面板 `未启动`，无错误；`mergeTeamEvent` 无该分支。

**根因**：

- `frontend/src/App.tsx:51-92`：`mergeTeamEvent` 只在 `team_start` 时建立快照；`if (!prev || !prev.team) return prev`（第 66 行）会把 `team_unavailable` 直接丢弃。
- `frontend/src/components/TeamPanel.tsx:27` 虽定义了 `team_unavailable: '团队不可用'`，但 `team` 为 null 时面板渲染的是 empty state，不会展示事件。
- `prepare_turn_team`（`backend/app/subagent/session.py:162-167`）失败后只 set `turn.team=None` + 发事件，没有服务端日志。

**建议修法**：

- `mergeTeamEvent` 增加分支：`team_unavailable` 时返回一个「events-only」快照（`team: null, tasks: [], messages: [], events: [event]`），或维护独立的 `teamError` state。
- `TeamPanel` 在 `!team` 时若收到 `team_unavailable`，显示红色提示：`Agent Team 不可用：{payload.error}` + 「本轮普通对话不受影响」。
- App 层可同时 `setError()` 一次（注意 4 秒自动消失；面板内应保留）。

**验收**：泄漏状态下发送消息，面板出现明确错误（不是「未启动」）；恢复配额后错误消失、正常显示 Team。

---

### BUG-05（P2）非法 mcp.json 的错误被吞

**现象**：保存 `{ invalid json` 后，后端返回 `200 {ok:false, errors:["JSON 解析失败: ..."], ...}`；前端只显示中性 notice「解析到 0 个 server…忽略 1 个无效项」，不显示 `errors`。

**证据**：`frontend/src/components/SettingsModal.tsx:342-356`（`saveJson`）；截图 `shots/05-mcp-invalid.png`；复验仍在。

**建议修法**：

```ts
const r = await api.mcpImport(jsonText)
if (!r.ok || (r.errors && r.errors.length > 0)) {
  setError('导入失败：' + (r.errors?.join('；') || r.message || '未知错误'))
  // 可选：仍展示 added/updated/skipped 的明细
} else {
  setNotice(r.message || '导入完成')
}
```
注意 `settings-section` 已同时渲染 `.auth-error` 与 `.mcp-notice`，用 error 通道即可。

**验收**：非法 JSON → 红色错误框含「JSON 解析失败」；合法 JSON → 绿色/中性 notice + 列表更新；部分成功（added>0 且 errors>0）两者都显示。

---

### BUG-06（P2）低阈值自动压缩每轮重复触发

**现象**：`context_window=8000, auto_compact=1200, hard=1800` 时：

- 第 4 轮：`before=1544 → after=1235`（>1200）。
- 第 5 轮：`before=1846 → after=1419`（>1200）。
- 第 6 轮：`before=1446 → after=1413`（>1200），每轮继续 `auto`。
- `/context` 返回 `remaining_to_compact=0`、`compacted=true`。

**证据**：`_test_soft_compact.py`、`_test_hard_trim.py`、`_recheck_bugs.py`；`docs/20261104-上下文压缩方案与进度.md` §7.1 已记录该遗留。

**根因**：

- `backend/app/compaction.py:143-188`（`enforce_soft_limit`）：先按 `target = soft * 0.5` 选保留尾部，但 `after = message_token_counter(preserved) + summary_tokens` 没有回检查；摘要本身可能就占 `soft` 的一半，导致 `after > soft`。
- `backend/app/routers/chat.py:144-159`：每轮先软线、后硬线；软线压缩后若仍高于硬线，本轮不会再裁剪（硬线检查在软线之后，但读的是压缩后的历史——本次 after 1419 < hard 1800，所以不触发硬线）。
- `AUTO_MIN_KEEP_MESSAGES=2` 与 `_safe_keep_from` 会阻止继续丢弃，保留尾部无法再小。

**建议修法**（择一或组合）：

1. 压缩后强制回检查：`while after > soft and len(preserved) > AUTO_MIN_KEEP_MESSAGES:` 把最旧的 preserved 并入再摘要/或直接丢弃（注意不要留下孤儿 tool 结果，用 `_safe_keep_from`）。
2. 给摘要本身设 token 上限（如 `max(200, min(1000, int(soft*0.35)))`），并在 prompt 里明确「不超过 N 字」；`LLMClient.run` 支持 `max_tokens` 的话直接用。
3. 记录「本轮已压缩过」标记（或在 session 上存 `last_compacted_at`/`last_compacted_tokens`），同一内容不重复压缩；至少把 `after_tokens` 写回前端事件，便于诊断。
4. 硬线优先：在软线前先判断硬线并硬裁，避免「软线摘要完仍超软线」的循环。

**验收**：

- 低阈值会话连续 4–5 轮后，`/context.used_tokens <= auto_compact_tokens`，不再每轮出现 `compaction(trigger=auto)`。
- 压缩后仍能回答最近上下文（保留最近 2–4 条）；摘要卡计数与实际一致。
- 不出现孤儿 `tool` 消息（`_safe_keep_from` 场景）。

---

### BUG-04（P2→P3，偶发）停止后 Team 显示「已过期」

**现象**：曾观测到 UI 停止后 Team 面板显示「已过期」，DB `team_runs.status='stale'`（会话 `co_b3d7a66462cb`，截图 `shots/09-after-stop.png`）。

**18:53 复验**：干净配额用户 `claude6` 走 abort+stop 后，generation/team 均为 `stopped`，未复现 stale。旧会话是 generation 与 team 双 `stale`，说明当时 stop 端点没有收敛成功（疑似 generation_id 不匹配/竞态）。

**建议**：

- 与 BUG-02 一起修：stop 端点对「按 DB active 代没收敛到」的情况加兜底（例如再按最新 active team 行收敛）。
- `routers/team.py:37-59` 的 `_team_scope` 在 `has_active_generation=false` 时，先看 `session_generations` 最近状态：`stopped` → 写 `stopped`，只有真正无主才写 `stale`。
- 验收：UI 停止后 250ms 的 `GET /team` 状态为 `stopped`；重复 10 次不出现 stale。

---

### BUG-07（P3）顶栏标题被挤压

**18:53 现状**：1280px + Team 展开：标题 344px（可读，仅尾部省略）；1024px + Team 展开：标题 118px（约 6 字），仍截断。

**相关**：

- `frontend/src/styles/index.css:139`：`.main-header .title { flex: 1; overflow: hidden; ... }` 是唯一可收缩项，自动最小尺寸为 0。
- `index.css:479-484` 模型选择器 `max-width: 260px`；`647-704` 的 720/900 断点。
- Team 面板展开宽度（`index.css` 约 791/907 行，`width: 290/300px`）。

**建议**：

- `.main-header .title { min-width: 160px; }`；
- `@media (max-width: 1100px)` 隐藏 badge / 工具状态，或把模型选择器压到 `150px`；
- Team 展开时把「压缩上下文」收进图标菜单；
- 验收：1024px + Team 展开，长标题至少显示 10 个中文字符且不挤压其它控件。

---

### BUG-08（P3）移动端会话列表半行裁切

**现状**：400×800，`.session-list` 可视区内仍有一项 `partial: true`（顶部在内、底部超出），看起来像渲染断裂；截图 `shots/31-recheck-mobile.png`。

**相关**：`frontend/src/styles/index.css:647-676`（`@media max-width:720`），`.session-list { max-height: 17vh; }`（654 行），`.sidebar { max-height: 27vh; }`。

**建议**：把 `max-height` 设为「整项高度的整数倍」（如 `calc(4 * 56px)`），或给列表加 `scroll-snap-type` / 底部渐隐遮罩；验收：任意 360–480px 宽度下，列表边缘不出现半行文字。

---

### BUG-09（P3）登录页密码框不在 `<form>` 内

**现状**：`frontend/src/components/LoginPage.tsx:61` 是 `<div className="auth-form">`，第 104-113 行密码框靠 `onKeyDown` 手写 Enter；Chrome 控制台持续告警 `Password field is not contained in a form`。

**建议**：改为 `<form onSubmit={(e)=>{e.preventDefault(); submit()}}>`，密码框加 `name="password"`，按钮 `type="submit"`；保留现有错误提示与 busy 状态。验收：控制台不再出现该告警，Enter 仍可提交，密码管理器可保存。

---

### BUG-10（P3）生成中上下文条显示 0.0k

**现状**：新建空白会话发送长文，0.4s / 2.4s 采样均显示 `0.0k / 400.0k · 0.0%`；结束后才刷新。`frontend/src/App.tsx` 只在 `loadContextUsage` 的调用点更新（181-187；264/337/380/588/610），流式期间没有更新。

**建议**：

- 方案 A（推荐）：流式期间用本地估算（按消息/增量字数）更新显示，结束时用 `/context` 校准；或显示「统计中…」而不是 0.0k。
- 方案 B：`message_start` 后、每个工具/文本段后节流调用 `/context`（注意别打爆后端）。
- 验收：生成中数值单调增长或显示「统计中…」，不出现「有对话但 0.0k」。

---

### BUG-11（P3）压缩提示与摘要卡计数不一致

**现状**：`POST /compact {keep:3}` 响应 `message="…保留最近 3 条"`，摘要块 `kept_messages=4`（含摘要卡本身）；用户看到两个数字。

**相关**：`backend/app/compaction.py:133-140`（`kept = len(preserved) + 1`，message 用 `len(preserved)`），`64-83`（`_record_visible_summary` 写 `kept`）。

**建议**：统一口径，例如 message 改为「保留最近 3 条 + 1 条摘要」，或 `kept_messages` 也改成 `len(preserved)` 并在卡片文案注明「不含摘要卡」。验收：提示与卡片数字可互相解释，测试 `_test_compact2.py`。

---

### BUG-12（P3）会话 PATCH 校验缺失

**现状**：`PATCH /api/sessions/{id}` 传不存在 `model_id` 返回 200 并保存；空白标题 `"   "` 原样保存。

**相关**：`backend/app/routers/sessions.py:123-128`（`update_session`），`schemas.py::UpdateSessionIn`。

**建议**：

- `model_id` 非空时 `db.get_llm_config(user_id, model_id)` 校验，失败 400/404；空字符串保留为「跟随默认」。
- `title` 保存前 `strip()`；空则拒绝 400 或回退「新会话」。
- 验收：`_suite_api.py sessions` 增加负向断言；UI 下拉不再显示「（该配置不可用）」。

---

### BUG-13（P3，待确认设计）每轮都创建 Team

**现状**：普通「只回答 OK」也会创建 Team（`completed`，0 任务 0 消息）；面板显示「已完成」。

**相关**：`backend/app/subagent/session.py:63`（每轮生成 `team_id`）、`162-167`（`prepare_turn_team` 每轮 `create`）；`chat.py` 的 prelude 每轮调用。

**建议（二选一，需产品确认）**：

- 保持设计，但面板在「0 任务 0 消息」时显示「本轮未发生协作」，而不是空任务板 + 已完成；
- 或延迟建 Team：第一次调用 team/subagent 编排工具时才 `create()`（注意 quota/lifecycle 的锁粒度与失败路径）。
- 验收：普通对话的面板文案符合产品预期；真正协作时任务/消息正常。

---

### BUG-14（P3）localStorage 偏好跨账号共享

**现状**：`frontend/src/App.tsx:11` `TEAM_OPEN_KEY = 'team_panel_open'`；claude2 展开后登出，登录全新 claude7，面板直接 `is-open`（复验通过）。

**建议**：key 加用户维度，如 `team_panel_open:${user.id}`；或在登录/登出时清理该 key。验收：切换账号后使用各自偏好。

---

## 3. 建议补的回归测试

| 类型 | 用例 | 期望 |
|---|---|---|
| 单测 | `create_child` 在 `team_id=""` 且父带 team 时，`ctx.role == "teammate"` | 覆盖 BUG-01 |
| 单测 | `TeamHost.release_lifecycle_now()` 幂等；`Lease.release()` 两次不重复 release | 覆盖 BUG-02 基础 |
| 单测 | `_event_stream` 清理段在 `close_all` 抛 `CancelledError` 时仍释放 team lease（fake host/lease） | 覆盖 BUG-02 核心 |
| 集成 | 发消息→读 200 字节→断连→新会话发送 | `team_start`（不是 `team_unavailable`） |
| 集成 | abort + `/stop` 后同会话再发 | 无 409；team 状态 stopped |
| 集成 | 断连后 `hub` 无残留 host；子代理任务已取消 | 可用内部断言/日志 |
| 前端 E2E | 非法 mcp.json → 红色错误；team_unavailable → 面板错误 | BUG-05/03 |
| 前端 E2E | 400px 列表无半行；1024+Team 长标题 ≥10 字 | BUG-08/07 |
| 前端 E2E | 流式中上下文条不为 0.0k | BUG-10 |

---

## 4. 测试环境、账号与证据

- 测试账号（本机）：`claude` / `claude2` / `claude3`–`claude7`，密码均 `12345678`。
- **当前 `claude2/5/6` 处于 Team 配额泄漏态**（BUG-02 的表现）；重启后端进程可清空，或换新用户复验。
- 真实模型：仅「湛卢」`moma/deepseek-latest-flash`（`https://ecloud.10086.cn/api/query/aigateway/v1`，`openai_chat`，窗口 400k）。故障路径用不可达 `http://127.0.0.1:1/v1`，未调用其它真实 API。
- 证据目录：
  - `work/claude_test_and_review/shots/`（01–35 截图）
  - `work/claude_test_and_review/runs/api_*/`（SSE 原始帧、断言结果）
  - `work/claude_test_and_review/_suite_api.py` 与 `_test_*.py` / `_recheck_*.py`（复跑脚本）
- 完整清单与复验记录：`docs/20261106-Web自测Bug清单.md`。

---

## 5. 已知非 bug / 环境说明（不要误修）

1. **`playwright-cli reload` 偶发停在 `about:blank`**：发生在 `frontend/dist` 重建窗口内，重新 `open http://127.0.0.1:8000/` 即恢复；不是应用白屏。真正的「停止后继续发送」白屏未复现。
2. **从仓库根运行 `pytest backend/tests` 会 `ModuleNotFoundError: app`**：这是测试路径问题，需 `cd backend && python -m pytest tests -q`（或 `PYTHONPATH=backend`）。不算产品 bug，但可以顺手加 `backend/tests/conftest.py` 或 `pytest.ini` 改善 DX。
3. **模型选择器的 `reasoning_effort` 为空时显示 `default`**：这是当前 UI 的显式设计（来自 `reasoning_effort` 字段），不是缺失。
4. **普通对话在 Chat 里出现 `team_event: team_start`**：P3 既定设计（每轮隔离 Team），见 BUG-13；如果要改需产品确认。
5. **中文控制台输出乱码**：测试脚本在 Windows 终端输出中文可能显示 `??`，不影响 DB/API 内容（已验证 UTF-8）。

---

## 6. 变更记录

- 2026-11-06 19:0x：首版修复须知，基于 18:53 复核结果与当前代码行号；P0 两项给出具体修法与验收命令。

# Agent Team 侧栏展开状态稳定修复

> 日期：2026-11-06 · 状态：已完成 · 范围：前端 TeamPanel / team SSE 事件解析

## 1. 问题

用户点开右侧 Agent Team 面板后，点击发送进入新一轮生成，面板会消失；直到本轮生成结束才重新出现。
表现为“面板一会开一会关”，用户无法在执行期间持续观察任务板。

## 2. 根因

定位到四个叠加问题：

1. `App.send()` 开头执行 `setTeamSnapshot(null)`，新一轮开始即把 Team 数据清空。
2. `TeamPanel` 以 `if (!snapshot || !team) return null` 提前返回，数据被清空时整个 `<aside>`
   （包括 rail 和展开壳）一起卸载；因此看起来像“自动收起”。
3. 顶部 `Team` 按钮只在 `teamSnapshot?.team` 存在时渲染，数据为空时入口也消失。
4. `team_event` SSE 的解析协议有错位：后端 `data` 本身是持久化 Team 事件（业务 `type` 为
   `team_start/task_created/...`），前端 `parseFrame` 若保留业务 type，就匹配不到 `ev.type === 'team_event'`；
   若覆盖为协议名，又会丢失业务 type。结果 `team_start` 不能实时填充面板，只能等 `finally`
   里的 `loadTeam()` 在生成结束后补拉。

## 3. 修复

### 3.1 面板壳与数据解耦

`TeamPanel` 现在只在“用户收起且没有 Team 数据”时返回 null：

```tsx
if (!open && !team) return null
```

只要用户点开过，面板壳就始终存在。生成开始清空旧快照时显示稳定的等待态：

- 生成中：`正在等待 Agent Team 启动…`
- 未生成且无 Team：`当前会话尚未创建 Agent Team`
- 有 Team 后：正常显示任务 / 消息 / 动态

### 3.2 展开状态持久化

`teamOpen` 改为从 `localStorage['team_panel_open']` 初始化，并在每次切换时写回。
因此：

- 同一页面内跨生成轮次不会自动收起；
- 刷新页面后仍保持用户上次的展开选择；
- 只有用户点击 rail / Team 按钮才会改变。

### 3.3 修复 `team_event` SSE 协议

`parseFrame` 对 `team_event` 与 `subagent_event` 采用同一包装方式：

```json
{ "type": "team_event", "event": { "type": "team_start", ... } }
```

`StreamEvent` 类型同步为 `{ type: 'team_event'; event: TeamEvent }`，`App` 用
`mergeTeamEvent(prev, ev.event)` 合并真实业务事件。

### 3.4 新一代 Team 替换旧快照

`mergeTeamEvent` 对 `team_start` 做了显式换代：即使 `prev` 里还有上一轮的旧 Team，
只要 `team_id` 不同，就用新 `team_start` 重建快照，不再被旧的
`prev.team.team_id !== event.team_id` 守卫拦掉。

### 3.5 视觉细节

新增无 Team 时的等待态样式：面板头部渐变、虚线占位卡、等待标题层级；
不新增气泡，保持 P3 的设计语言。展开宽度仍为 330px / 响应式 300px。

## 4. 验证

### 4.1 构建

- `npx tsc --noEmit` 通过。
- `npm run build` 通过（`dist/assets/index-DFqNDRqC.js`）。

### 4.2 后端回归

`backend: python -m pytest -q`：

```text
119 passed, 1 warning
```

### 4.3 真实浏览器自测（Playwright，8000 端口）

1. 打开已有 Team 的会话，点击展开 → 面板正常显示。
2. 刷新页面 → 仍保持展开。
3. 发送真实消息，在流式期间连续采样 40 次（2 秒）→ `.team-panel.is-open` 始终存在。
4. `team_start` 到达后、生成尚未结束时，状态从“等待启动”切换为运行态。
5. 点击停止后 → 面板仍保持展开。
6. 新建空会话 → 面板保持展开并显示“当前会话尚未创建 Agent Team”。

全部通过。

## 5. 风险与后续

- `localStorage` 仅保存一个布尔展开偏好，不保存当前会话选择；多会话切换时面板状态一致。
- Team 事件回放仍以 `GET /api/sessions/{id}/team` 为准，本次未改变持久化/隔离语义。
- 若后续支持移动端抽屉式 Team 面板，需要把 `open` 与 viewport 断点策略拆开，避免与
  `@media (max-width: 720px) { .team-panel { display:none } }` 冲突。

## 6. 变更记录

- 2026-11-06：完成定位、修复、单元测试与真实浏览器 E2E 验证。

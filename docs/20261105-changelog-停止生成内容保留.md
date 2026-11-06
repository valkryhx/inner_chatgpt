# 停止生成后保留已生成内容

> 日期：2026-11-05
> 状态：已完成
> 范围：聊天 SSE 停止链路、assistant 消息持久化、前端停止态合并

## 1. 问题与根因

用户点击“停止”后，本轮已经显示在页面上的回复消失，只剩：

```text
（未生成回复，可能已中断）
```

数据库中该 assistant 消息为：

```text
status=stopped
content=""
reasoning=""
blocks=[]
```

根因是两层叠加：

| 层 | 问题 |
|---|---|
| 后端持久化 | 只在整轮结束/取消时写入一次完整快照；取消恰好发生在写库前时，空占位被保存 |
| 前端停止 | 立即 `abort()` SSE；服务端尚未产生最终 `done`，随后历史刷新用空占位覆盖本地已显示内容 |
| 空态判断 | 只检查 `content` 和少数块类型，reasoning/subagent 等已产生内容仍可能被误判为空 |

## 2. 修复设计

### 2.1 后端：流式增量快照

`backend/app/routers/chat.py`：

- 文本、思考、工具、图表、子代理事件到达时，先更新本轮内存块，再节流写库（250ms）；
- 取消、异常、`finally` 一律 `force=True` 写入最后快照；
- 终态只写一次：`done(complete/stopped)`、流内 `error`、取消、异常各自锁定状态，
  `finally` 仅在前述路径未落终态时兜底为 `stopped`，防止正常完成被覆盖；
- `stop` 端点接受 `message_id`，停止后等待短暂时间取得已落库消息，
  通过 `StopResult.message` 直接返回给前端；
- 旧客户端不传 `message_id` 时保持原有停止行为。

### 2.2 前端：停止后不回退

`frontend/src/App.tsx`：

- 点击停止时先保留本地已渲染消息；
- 调用 stop 端点获取服务端快照，匹配到同一消息后落为 `stopped`；
- 停止后的历史刷新采用合并策略：服务端快照为空但本地已有内容时保留本地，
  避免异步写库窗口造成内容消失。

### 2.3 显示层

`frontend/src/components/MessageList.tsx`：

- `isEmptyNoLive` 改为检查所有可见块（text/reasoning/tool/chart/summary/subagent）；
- 有可见块显示“（已停止）”；
- 真正无内容时只显示“（已停止，未生成内容）”，不再同时显示含歧义的“可能已中断”；
- `done(status=error)` 映射为错误态，避免错误终态被前端误标为正常完成。

## 3. 测试与验证

### 3.1 后端单测

新增：

- `test_stop_persists_partial_text_before_done`：收到一段文本后模拟取消，最后快照必须包含该文本；
- `test_stop_result_returns_persisted_snapshot`：stop 接口返回已落库快照；
- `test_complete_terminal_status_is_not_overwritten`：正常完成仍为 `complete`；
- `test_upstream_error_terminal_status_is_not_overwritten`：流内错误仍为 `error`；
- `test_cancel_terminal_status_is_stopped`：取消仍为 `stopped`。

回归：

```text
79 passed, 1 warning
```

### 3.2 真实浏览器 E2E

任务：生成约 1200 字技术随笔，在出现正文后点击停止。

第一次验证：

```text
停止前 32 字 -> 停止后 32 字 -> 刷新后 32 字
“已停止”=1，“未生成回复”=0，console errors=[]
```

合并刷新补丁后复测：

```text
停止前 89 字 -> 停止后 113 字 -> 刷新后 113 字
“已停止”=1，“未生成回复”=0，console errors=[]
```

数据库对应 assistant 消息：

```text
status=stopped
content=已生成的正文
blocks=[{"type":"text","text":"已生成的正文"}]
```

截图：`work/stop_preserve_after.png`。

终态保护补充后复测：

```text
停止前 17 字 -> 停止后 28 字 -> 刷新后 28 字
“已停止”=1，“未生成回复”=0，console errors=[]
```

### 3.3 构建

```text
frontend: npx tsc --noEmit -> passed
frontend: npm run build -> passed
```

## 4. 兼容与边界

1. stop 接口新增可选字段 `message_id` 和响应字段 `message`，旧调用方无需修改；
2. 若服务端在等待窗口内仍未落库，前端保留本地快照，不以空历史覆盖；
3. 停止态不会重新触发模型请求，仍会级联停止 subagent；
4. 此修复不改变正常完成轮次的持久化语义。

## 5. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-05 | 修复停止后内容丢失：增量快照落库 + stop 返回消息快照 + 前端合并保留 + 空态判断修正；79 tests passed |
| 2026-11-05 | 补终态幂等保护：正常完成、流内错误、取消不会被 finally 覆盖；空停止消息只显示一次提示；真实停止 E2E 通过 |

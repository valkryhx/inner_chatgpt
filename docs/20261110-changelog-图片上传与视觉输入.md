# 图片上传与视觉输入 —— 实施记录（changelog）

> 日期：2026-11-10
> 设计：`docs/20261110-图片上传与视觉输入设计方案.md`
> 状态：已完成，真实模型 E2E 通过

## 1. 落地的能力

| 能力 | 入口 |
| --- | --- |
| 选图 / 粘贴 / 拖拽上传 | `Composer` 的 `+` 按钮、textarea `onPaste`、`.composer` 的 `onDrop` |
| 上传接口 | `POST /api/me/uploads`（multipart，字段 `file`） |
| 随消息发送 | `POST /api/sessions/{sid}/messages` 新增可选 `attachments: [{path}]` |
| 图片回看 | `messages.blocks` 里的 `{"type":"attachments","items":[...]}`，前端用既有 `/api/me/workspace-image` 鉴权渲染 |
| 多轮视觉 | 最近 3 轮用户消息里的图片仍会送模型；更早的不再重发，避免上下文膨胀 |

## 2. 存储

```text
backend/data/workspaces/<user_id>/uploads/<uuid>.<ext>
```

- 文件名由服务端生成（`uuid4` + 真实 MIME 推导的扩展名），**不采用用户文件名**，杜绝路径穿越与重名；
- 用户原始文件名只作为展示字段存进消息引用；
- 数据库只存引用，**历史里永远不存 base64**（已用 DB 查询核实）。

## 3. 代码落点

| 文件 | 改动 |
| --- | --- |
| `backend/app/uploads.py` | 新增：magic-bytes 判型、落盘、路径校验、data URL、`expand_images_for_wire` |
| `backend/app/routers/media.py` | 新增 `POST /api/me/uploads` |
| `backend/app/schemas.py` | `SendMessageIn.attachments`；`text` 改为可空（纯图片消息） |
| `backend/app/sse.py` | 用户消息带 `attachments` 进 `session_history` |
| `backend/app/agent_loop.py` | 送模型前用副本展开图片，原 history 保持纯文本 |
| `backend/app/llm_client.py` | `openai_chat` → `image_url` 块；`openai_responses` → `input_image` 块 |
| `frontend/src/components/Composer.tsx` | 选图/粘贴/拖拽、缩略图、移除、上传中禁用发送 |
| `frontend/src/components/MessageList.tsx` | 渲染用户附图 |
| `frontend/src/{api,types}.ts` | `uploadImage()`、`Attachment`/`UploadedImage` 类型、`streamMessage(..., attachments)` |

## 4. 真实 E2E（用户当前默认模型 `moma/deepseek-latest-flash`）

| 步骤 | 结果 |
| --- | --- |
| 上传 `probe42.png`（白底红字 42，240×120 重绘为 300×160） | `200`，`path=uploads/f41d…png`，`size=2844` |
| 带图提问「这张图里写的是什么数字？」 | 模型回复 `42` |
| 同会话再问一次（不带新图） | 仍答 `42` —— 说明历史里的图片在多轮中仍然可见 |
| **负对照**：新会话、同样的问题、不带图 | 模型回答「对话中没有图片附件，我看不到任何…」 |

负对照很关键：它排除了"模型瞎猜 42"的可能。

## 5. 测试

- 新增 `backend/tests/test_uploads.py` 15 个用例（落盘、判型、越界、大小、data URL、历史展开、两种 wire 格式、上传接口、纯图片消息）。
- 后端全量 **286 passed**；前端 `tsc --noEmit` 干净、`npm test` 4 passed。

## 6. 已知限制

1. 单条消息最多 4 张图（`MAX_IMAGES_PER_MESSAGE`），单张 25MB。
2. 只有最近 3 轮用户消息的图片会送模型；更早的图退化为不可见（预算控制）。
3. 删除消息/会话**不会**删除 `uploads/` 里的文件（后续 GC）。
4. A2A 暂不支持 `file` part 传图。
5. token 估算仍按纯文本，图片不计入上下文百分比。

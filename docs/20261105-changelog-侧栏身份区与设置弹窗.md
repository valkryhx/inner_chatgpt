# 侧栏身份区重做 + 设置弹窗定高 —— 完成记录

> 日期：2026-11-05 · 状态：已完成
> 关联：`docs/20261105-前端审美提升设计.md`（5.x 侧栏 / 6.2 设置弹窗）

## 一、背景

侧栏左下角原来是「圆形字母头像 + 名字」加两个独立小按钮（设置 / 登出），存在三个问题：

1. 字母缩写（如 `H` / `T`）不是有效身份信息，用户明确要求用全名。
2. 「设置」与「登出」权重相同，视觉上像两个并列的主操作，噪音大。
3. 进入设置必须点小按钮，点击名字没有反应，不符合直觉。

另外设置弹窗只设了 `max-height`、没有固定高度，三个 tab（APIKey / MCP / Skills）内容长短不同，
切换时弹窗**忽大忽小**，很不雅。

## 二、改动

### 2.1 侧栏身份区（`Sidebar.tsx` + `index.css`）

- 删除字母头像，改为 **全名主标识 + 邮箱副标题**：
  - `displayName = user.name || user.username || user.email || '未命名用户'`
  - 仅当邮箱与名字不同时，才把邮箱作为第二行小字；窄屏（≤900px）隐藏邮箱行。
- **整行变成按钮 `.user-chip`**：点名字 = 打开设置，符合直觉。
  - 左侧 26px 圆形线性人物图标（`--clay-soft` 底 + `--btn` 描边色），不再用字母。
  - 右侧齿轮图标 `.user-gear`：默认 `opacity .38` 常驻淡显，提示「这行可点」，hover 变实。
- **登出降级为次要动作 `.user-logout`**：默认灰字，hover 才转 `--danger` + 浅红底；图标 + 文案。
- 全部图标改为内联 SVG（人物 / 齿轮 / 登出），无 emoji。

### 2.2 小屏可用性修正

原 `≤720px` 规则把整个 `.sidebar-footer` 隐藏，导致手机上**没有任何入口**进设置/登出。
改为：小屏只隐藏「模型 + 工具数」状态行，保留身份 + 登出这一行；隐藏邮箱副标题与「登出」文字（只留图标），
避免挤压。

### 2.3 设置弹窗定高（`index.css`）

```css
.modal {
  width: 720px;
  height: min(86vh, 760px);       /* 固定高度，切 tab 不再跳动 */
  max-height: calc(100vh - 48px);
}
.modal-head, .modal-tabs { flex-shrink: 0; }
.modal-body { flex: 1 1 auto; min-height: 0; overflow-y: auto; }  /* 内容区内部滚动 */
```

小屏（≤720px）底部抽屉同步改为 `height: 92vh`。

## 三、验证

Playwright 真浏览器（登录 test / 12345678），依次点击三个 tab 测量 `.modal` 尺寸：

| 视口 | APIKey | MCP | Skills | 高度变体数 | 宽度变体数 |
|---|---|---|---|---|---|
| 1440×900 | 720×760 | 720×760 | 720×760 | 1 | 1 |
| 390×844 | 366×776 | 366×776 | 366×776 | 1 | 1 |

- 侧栏身份区：1440/1024 显示「test / 123@qwe.com」，768/390 只显示「test」（邮箱按预期隐藏）。
- 点名字 → `.modal` 出现（4 档位均 `modal: 1`）。
- 无横向溢出（`scrollWidth == clientWidth`），控制台 0 error。
- `npm run build` 通过。

截图：`misc/design-preview/batch3/ub-final-*.png`、`misc/design-preview/batch4/modal-*.png`。

## 四、注意

- `.avatar` / `.user-name` / `.user-info` / `.user-actions` 这些旧 class 已不再使用，CSS 中已一并移除；
  若后续要复用侧栏头像请重新定义，不要依赖旧的单字母样式。

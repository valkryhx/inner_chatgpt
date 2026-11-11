# 搜索功能验证脚本

这组脚本用于验证 Hlaudz 的「历史对话搜索」功能，以及它没有破坏既有功能。
**可以随时重跑**，明天演示前建议至少跑一遍 `regress_ui.py`。

## 共同约定

每个脚本都会自己：

1. 在 `work/<名字>_db/` 复制一份生产库（**绝不碰 `backend/data/app.db`**）；
2. 起一个**独立端口**的后端实例（不干扰你正在跑的 8000）；
3. 造测试账号与数据，跑完自动杀掉进程。

所以可以并行跑、反复跑，不会污染真实数据。

前置：`pip install playwright && playwright install chromium`（已装可跳过）。

## 脚本清单

| 脚本 | 验证什么 | 耗时 | 什么时候跑 |
|---|---|---|---|
| `regress_ui.py` | **39 项 UI 回归**：登录、侧栏、会话切换、设置 4 个 tab、会话浏览器、文件面板、输入框、快捷键、连开 12 次、全程无白屏 | ~90s | **演示前必跑** |
| `attack_whitescreen.py` | 用 10 种畸形服务端响应攻击搜索面板，确认不白屏 | ~60s | 改了搜索前端之后 |
| `error_boundary.py` | 证明 ErrorBoundary 能兜住渲染崩溃且可恢复 | ~40s | 怀疑白屏防护失效时 |
| `responsive.py` | 7 种分辨率（1920 到 320）下面板不溢出、不白屏 | ~90s | 演示用投影/小窗时 |
| `demo_flow.py` | **真实 LLM**：发一句话 → 搜索这句话 → 点击跳转，全链路 | ~60s | 演示前，确认模型链路通 |

## 跑法

```bash
cd /d/git_codes/inner_chatgpt
python tools/search_verify/regress_ui.py
python tools/search_verify/attack_whitescreen.py
python tools/search_verify/responsive.py
python tools/search_verify/error_boundary.py
python tools/search_verify/demo_flow.py      # 这个会真的调 LLM
```

退出码 0 = 通过。截图落在 `work/<名字>_shots/`。

## 关键设计说明

### 为什么这些脚本不测「搜索能搜到东西」

那个用 `backend/tests/test_session_search*.py` 的单测覆盖（51 项）。
这里专门测**单测测不到的东西**：

- 单测能证明 `search_messages()` 返回正确，不能证明「点搜索按钮后页面没白屏」；
- 单测能证明字段类型，不能证明「服务端返回畸形数据时前端不炸」；
- 单测完全不涉及渲染、路由、事件绑定。

### 白屏是怎么被攻破、又是怎么修好的

`attack_whitescreen.py` 最初跑出 **4 个真实白屏**：

| 畸形响应 | 崩溃信息 |
|---|---|
| `hits` 不是数组 | `Cannot read properties of null (reading 'length')` |
| `hits` 是对象 | `a.map is not a function` |
| `snippet` 为 null | `Cannot read properties of null (reading 'length')` |
| `hits` 里含 null 元素 | `Cannot read properties of null (reading 'role')` |

项目原本**没有任何 ErrorBoundary**，所以任何一次渲染异常都等于整页空白。
修复分两层，且**各自独立有效**（都做过反向确认）：

1. **数据入口 `normalizeResult()`** —— 不信任响应形状，逐字段兜底；
2. **`ErrorBoundary`** —— 兜住一切漏网的渲染异常，降级成可恢复面板。

`error_boundary.py` 通过往渲染路径注入 `throw` 证明第 2 层真的生效：
降级面板出现、侧栏和输入框仍在、点「重试」能恢复。
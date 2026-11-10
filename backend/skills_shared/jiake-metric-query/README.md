# 家客指标查询 Skill 包

> `jiake-metric-query` —— 家客（家庭客户）业务指标查询的静态操作手册。
> 面向 sqlbot MCP，但与 agent / MCP 实现解耦：只描述「业务语义 → 工具参数」的映射。

## 这是什么

一套让智能体在**家客经营数据查询场景**下少走弯路的技能包。它在系统提示词里只占一行索引，
匹配到任务时才加载完整正文（渐进披露），因此不影响日常对话的开销。

解决的问题：

- 模型不知道 635 个指标里哪个对应"千兆占比"，容易编 id；
- 不知道 `metric_compare` 的 A/B 方向、`zone_names` 多值缺陷，会把结果说反/说空；
- 不知道占比是 0–1 小数，会把 39.7% 说成 0.4%；
- 会用业务恒等式"校验"演示数据，误报"数据有问题"；
- 不知道什么时候该用模板、什么时候才该走只读 SQL 逃生口。

## 目录结构

```
jiake-metric-query/
├── SKILL.md                       # 主文件：三步流程、工具矩阵、口径陷阱、回答结构、自查清单
├── references/
│   ├── data-shape.md              # 三表结构、时间区间、区域层级（含"实际列比逻辑 schema 少"）
│   ├── metric-catalog.md          # 635 指标的业务方向导航 + 检索技巧
│   ├── tool-contract.md           # 12 个工具的入参/出参/边界/已知缺陷（实测）
│   └── playbooks.md               # 10 个常见问法的可照抄工具链剧本
└── assets/
    ├── metric-catalog.csv         # 全量指标 id,metric_name_cn 快照
    └── zone-catalog.csv           # 全量区域 zone_id,name,parent,level 快照
```

## 设计原则

1. **形态解耦，内容相关。** 包里没有任何脚本、没有 MCP 地址、没有 agent 名称，
   只有业务口径与工具契约。MCP 升级或换实现时，只需更新 `references/tool-contract.md`。
2. **渐进披露。** `SKILL.md` 是索引后最先读的正文；细节放 `references/`，
   模型按需用 `file_read` 读（`${SKILL_DIR}` 会在加载时替换成实际路径）。
3. **快照与实时分离。** `assets/*.csv` 是某一时点的静态快照，
   只用于**猜关键词**，落参数前必须用 `metric_search` / `zone_list` 实时确认。
4. **写实测，不写想象。** 文档里的每条"已知缺陷"与每个示例数值都来自真实调用，
   不是从代码推测的。

## 安装方式

三种都支持，按场景选：

### A. 放到公用技能层（推荐，所有用户可用）

```bash
cp -r jiake-metric-query backend/skills_shared/
```

重启后端即可，无需导入操作。公用层是只读的，用户不能从界面改坏它。

### B. 通过界面导入 zip

设置 → 我的 Skills → 「选择 zip 导入」。同名的用户私有技能会覆盖公用版本。

### C. 放到内置层（随仓库发布）

```bash
cp -r jiake-metric-query backend/skills_builtin/
```

## 前置条件

- 目标用户已配置 sqlbot MCP（`http` transport，端点 `http://<host>:8765/mcp`）；
- 该 MCP 暴露 12 个工具（`health` … `run_readonly_sql`）。

MCP 未连接时，`SKILL.md` 剧本 J 明确要求模型**如实告知而不是编造或改用 code_run**。

## 维护

数据或工具契约变化时，只需更新：

| 变化 | 更新文件 |
|---|---|
| 换库 / 数据区间变 / 区域增减 | `references/data-shape.md` + `assets/*.csv` |
| 指标字典增删 | `references/metric-catalog.md` + `assets/metric-catalog.csv` |
| 工具参数或返回结构变 | `references/tool-contract.md` |
| 发现新的口径陷阱 | `SKILL.md` §3 + 对应 `playbooks.md` 剧本 |

重新生成快照（需能访问 sqlbot 的库或 MCP）：

```bash
# 指标目录
python -c "
import sqlite3
con=sqlite3.connect('<path>/data/sqlbot.db')
rows=con.execute('select id,metric_name_cn from ads_jk_business_metric_dictionary order by id')
print('id,metric_name_cn')
for i,n in rows: print(f'{i},{n}')
" > assets/metric-catalog.csv
```

## 版本

- `2026-11-10` 首版。基于 `sqlbot_mcp` 演示数据集与 12 个工具的实测行为整理。

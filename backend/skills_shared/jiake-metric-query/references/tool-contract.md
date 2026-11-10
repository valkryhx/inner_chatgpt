# sqlbot MCP 工具契约（实测）

> 以 `sqlbot_mcp` 12 个工具的真实返回为准整理。**入参/出参与已知缺陷都在这里**，
> 遇到与本文不一致的返回，以运行时返回为准并在回答里说明。

## 0. 通用

- 传输：HTTP MCP，端点形如 `http://<host>:8765/mcp`。
- 工具名不带命名空间；平台会以 `[MCP: <server name>]` 前缀写进工具描述。
- 所有工具**只读**，不会修改数据。
- 返回体积：平台对 MCP 结果有统一预算（约 5 万字符）。
  超限会被裁剪，头尾各留约 2000 字符，中间替换为
  `… [N chars omitted; full output: <路径> (read with file_read)] …`。
  需要完整数据时用 `file_read` 读该路径，或**在 SQL/参数里先聚合**。

---

## 1. 元数据工具

### `health()`
无参。返回库连通性、库类型、三张表列名。**排查"连不上/表不存在"第一步**。

### `metric_search(keyword, limit=20)`
按中文名模糊搜索指标。

```json
{"keyword":"宽带","count":10,
 "items":[{"id":1,"metric_name_cn":"家庭宽带用户数","module_name":null,"unit":null}]}
```

- `module_name` / `unit` **恒为 null**（演示库无这两列）。
- `limit` 默认 20；要全量结果需要更大 limit（一次 1000 也可，但注意返回体积）。

### `metric_list(module=None, limit=200)`
按模块浏览。`module` 在演示库无效（无该列），实际等于"按 id 顺序取前 N 条"。
`limit=1000` 会返回全部 635 条（约 4 KB 以上，注意体积）。

### `zone_list(level=None, parent_id=None, keyword=None, limit=500)`
区域字典。

```json
{"count":1,"items":[{"zone_id":99,"zone_name":"全国","parent_id":0,
                     "zone_level":0,"level_name":"全国"}]}
```

- `level=0` 全国（1 条）、`level=1` 省（32 条）、`level=2` 市（16 条）。
- 按 `keyword` 模糊搜名称：`zone_list(keyword="广州")` → 广州市 1008。

### `time_options()`
无参。**查询前必须调一次**，确认数据区间。

```json
{"granularities":{"1":"天","2":"月","3":"季度"},
 "granularity_row_counts":{"1":0,"2":746760,"3":0},
 "granularities_with_data":[2],
 "earliest_data_time":"2024-08-01",
 "latest_data_time":"2026-07-01"}
```

> `granularities` 是**支持的枚举**；`granularities_with_data` 才是**真正有数据**的粒度。
> 本库只有 2（月）。此前用 `total_value_rows`（全表 count），
> 2026-11-11 改为按粒度分组，既省掉一次全表扫描，也直接回答"哪个粒度能用"。

### `dimensions()`
无参。一次给全貌：粒度、区域层级、逻辑表结构、`zone_count`。
注意返回的是**逻辑 schema**（带注释、列比实际多），写 SQL 前应改用 `table_schema()`。

### `table_schema(tables=None)`
返回**真实**表的列名/类型/主键（DB 反射）。

```json
{"tables":{
  "ads_jk_business_metric_values":{
    "table":"ads_jk_business_metric_values",
    "columns":[{"name":"data_time","type":"DATE","nullable":true}, ...],
    "primary_key":["dictionary_id","data_time","time_granularity","zone_id"]}}}
```

`tables` 可传逻辑 key（`dict`/`values`/`zones`）或真实表名；不传返回全部三张。

---

## 2. 查询工具

### `metric_series(metric_ids, zone_ids=None, zone_names=None, level=None, gran=2, month=None, start_month=None, end_month=None, limit=2000)`
**主力工具**。多指标 × 区域 × 时间序列。

```json
{"filters":{...},
 "columns":["dictionary_id","metric_name","zone_id","zone_name","data_time","metric_value"],
 "rows":[{"dictionary_id":1,"metric_name":"家庭宽带用户数","zone_id":99,
          "zone_name":"全国","data_time":"2026-07-01","metric_value":114124518.0}],
 "row_count":24,"truncated":false,"sql":"SELECT ..."}
```

关键行为（全部实测）：

| 参数 | 行为 |
|---|---|
| `month="2026-07"` | 只要该月 |
| `start_month`/`end_month` | 闭区间，如 `2024-08`~`2026-07` 返回 24 个月 |
| 不传时间参数 | 返回**全部**月份（24 行/指标/区域） |
| `zone_ids=[99]` | 全国 |
| `zone_ids=[106,108]` | 多区域，**OR**（各自返回） |
| `zone_names=["广东"]` | LIKE 模糊匹配（`%`/`_` 已转义，按字面量） |
| `zone_names=["广东","浙江"]` | **OR**（2026-11-11 修复；更早版本是 AND 会返回 0 行） |
| `zone_ids=[106,108]` 与 `zone_names` 等价 | 精确匹配，推荐 |
| `level=1` / `level=2` | 返回全部省 / 全部市 |
| `gran=1`/`gran=3` | 演示库中恒为 0 行（无天/季度数据） |
| `truncated` | 是否因 `limit` 被截 |
| `limit` | 会被夹到 `[1, 服务端上限]`；传 0 或负数回退默认值，**不会**变成"不限制" |

> 若同时传 `zone_ids` 与 `level`，两者是 **AND**，可能互相排除导致 0 行。一次只用一种。

### `metric_latest(metric_ids, zone_ids=None, zone_names=None, level=None, gran=2, month=None)`
取一个值。

- 不传 `month`：取**最新一期**（库内最新月）。
- 传 `month`：取该月；该月无数据返回 `rows: []`、`row_count: 0`（**不报错**）。
- `level=1` 时返回全部 32 个省在该月的值。

### `metric_compare(metric_ids, zone_ids, month_a, month_b, gran=2)`
两个月对比。

```json
{"rows":[{"dictionary_id":1,"metric_name":"家庭宽带用户数","zone_id":99,"zone_name":"全国",
          "value_a":114124518.0,"value_b":113959639.0,
          "delta":164879.0,"delta_pct":0.1447}]}
```

> ⚠️ **`delta = value_a − value_b`（A 为基准）**。
> 常规"本期环比对比期"表述要把**本期放 `month_a`**：
> `month_a="2026-07", month_b="2026-06"` → `delta=+164879`。
> 传反了会得到负号，容易误报"下降"。
>
> 区域参数：`zone_ids` / `zone_names` / `level` 至少给一个（2026-11-11 起支持后两者），
> 多值均为 **OR**。
>
> `delta_definition` 字段会明确回显 `delta = value_a - value_b`；
> `month_a == month_b` 时返回 `warnings`（delta 恒为 0）。

---

## 3. 图表工具

### `chart_spec(type=None, title=None, categories=None, series=None, x_name=None, y_name=None, unit=None)`
纯转换（不查库、不渲染）。

入参：

```json
{"type":"line","title":"全国家庭宽带用户数",
 "categories":["2026-01","2026-02"],
 "series":[{"name":"家庭宽带用户数","values":[112982266,113214721]}],
 "unit":"户"}
```

出参（成功）：

```json
{"type":"chart","spec_version":1,"chart_type":"line","title":"...",
 "option":{...ECharts option...},
 "data":{"categories":[...],"series":[{"name":"...","values":[...]}]},
 "meta":{"unit":"户","x_name":null,"y_name":null}}
```

失败：

```json
{"ok":false,"error":"series[0].values 长度(2) 与 categories 长度(1) 不一致"}
```

行为要点：

- `categories` 或 `series` 为空 → `ok:false`。
- 每个 `series[i].values` 长度必须 **等于** `categories` 长度。
- `values` 里的 `null` 原样保留（折线断开），字符串数字会被转成数值。
- `type` 不在 `line/bar/pie/table` 内会被忽略并自动推断。
- 自动推断规则：多序列 → `line`；单序列且类目像时间 → `line`；
  单序列且类目 ≤12 → `pie`；否则 `bar`；无序列 → `table`。
- 返回体带 `"type":"chart"`，平台的 SSE 层会据此识别成可渲染 artifact，
  前端直接出图——**所以正文不要再贴 option JSON**。

---

## 4. 逃生口

### `run_readonly_sql(sql, max_rows=1000)`
LLM 写只读 SQL。

成功：

```json
{"ok":true,"appended_limit":false,
 "columns":["zone_name","metric_value"],
 "rows":[{"zone_name":"内蒙古","metric_value":6207244.0}],
 "row_count":5,"truncated":false,"sql":"SELECT ..."}
```

被拒（校验不通过，**不是**执行出错）：

```json
{"ok":false,"rejected":true,
 "rejected_reason":"只允许单条 SQL，检测到 2 条（禁止分号拼接多语句）",
 "sql":"..."}
```

执行出错：

```json
{"ok":false,"rejected":false,"error":"OperationalError: no such column: x","sql":"..."}
```

校验规则（违反即 `rejected`）：

| 规则 | 说明 |
|---|---|
| 单语句 | 分号拼接多语句 → 拒绝 |
| 只读 | INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/TRUNCATE/GRANT/MERGE/COPY → 拒绝（含 CTE 夹带） |
| 表白名单 | 只能用 `ads_jk_business_metric_dictionary` / `ads_jk_business_metric_values` / `d_zone_info` |
| 自动 LIMIT | 没写 LIMIT 自动补，返回里 `appended_limit: true` |
| 行数上限 | `max_rows` 与服务器 `SQLBOT_MAX_ROWS` 取小，默认 1000 |

常见列名（**务必先 `table_schema()` 复核**）：

```
ads_jk_business_metric_dictionary: id, metric_name_cn
ads_jk_business_metric_values:     data_time, zone_id, dictionary_id, time_granularity, metric_value
d_zone_info:                       zone_id, zone_name, parent_id, zone_level
```

> 演示库的字典表**没有** `module_name` / `unit` / `is_delete`，
> 写 SQL 时用这些列会 `no such column`。

---

## 4.5 空结果与诊断字段（2026-11-11 新增）

`metric_series` / `metric_latest` / `metric_compare` 在 `row_count: 0` 时会带 `hints`：

```json
{"hints": ["指标 id 不存在: [999999]（用 metric_search 确认 id）"]}
{"hints": ["该库没有 gran=1 粒度的数据（用 time_options 看 granularities_with_data）"]}
{"hints": ["month='2020-01' 该期无数据（用 time_options 确认数据区间）"]}
```

**先读 hints 再下「没有数据」的结论** —— 空结果常是"口径不对"而不是"库里没有"。

其它诊断字段：

| 字段 | 出现场景 |
|---|---|
| `warnings` | 参数被静默忽略（如该库无 `module_name` 列）、同月对比、字典缺 `unit` 列 |
| `delta_definition` | `metric_compare` 恒定返回，写明 `delta = value_a - value_b` |
| `timeout` | `run_readonly_sql` 超时（与 `rejected` / `error` 并列的第三类失败） |

## 4.6 CTE 与只读 SQL 的能力边界（2026-11-11）

`run_readonly_sql` **支持 CTE**（`WITH a AS (...) SELECT ...`），
多步分析可以写。仍被拒绝的：

| 成分 | 原因 |
|---|---|
| `FOR UPDATE` / `FOR SHARE` | 会给行加锁，不是只读 |
| `PRAGMA` / `ATTACH DATABASE` | 元命令与附加库 |
| `load_extension(...)` 等 | SQLite 危险函数 |
| 多语句、写操作（含 CTE 内夹带 `DELETE`） | 基本红线 |
| 白名单之外的表（CTE 别名不算表） | 只能查三张业务表 |

失败返回三类，按字段区分：

| 字段 | 含义 | 该怎么做 |
|---|---|---|
| `rejected: true` | 校验不通过 | 读 `rejected_reason` 改 SQL |
| `timeout: true` | 超过 `SQLBOT_QUERY_TIMEOUT` | 缩小时间/区域范围，或先聚合 |
| `error: "..."` | 执行出错（列名不对等） | 读 error 改列名或条件 |

## 5. 参数校验失败时的读法

工具报错会以可读文本回到模型（平台侧统一处理），典型形态：

```
[工具执行错误] ValidationError: ...
[参数校验失败] 请严格按工具参数定义调用，不要臆造参数名。本次可用参数: [...]
```

收到这类错误时：**读「本次可用参数」列表，按真实参数名重试一次**，
不要原样重复同一调用。

会话级传输错误（`Session terminated` / `connection closed` 等）
平台会自动重连并重试一次，通常无需人工干预；若重连失败，如实告知用户该 server 暂不可用。

# 常见问法的工具链剧本

> 每个剧本给出**可照抄的工具调用序列**与**实测真实返回**（演示数据集，2026-07 为最新月）。
> 真实数值仅作「长什么样」的参照，回答时必须以本轮实际返回为准。

---

## 剧本 A：单指标单月单区域（最基础）

> 用户：「全国 2026 年 7 月家庭宽带用户数是多少？」

```
1. metric_search(keyword="家庭宽带")        → 确认 id=1, 名称"家庭宽带用户数"
2. time_options()                           → 确认 2026-07 在区间内
3. metric_latest(metric_ids=[1], zone_ids=[99], month="2026-07")
```

实测返回：

```json
{"rows":[{"dictionary_id":1,"metric_name":"家庭宽带用户数","zone_id":99,"zone_name":"全国",
          "data_time":"2026-07-01","metric_value":114124518.0}],"row_count":1}
```

回答要点：`114,124,518 户（约 1.14 亿户）`；说明"库内未登记单位，按量级判断为户"。

---

## 剧本 B：趋势（近 N 个月）

> 用户：「全国家庭宽带用户数今年以来的走势？」

```
1. metric_search(keyword="家庭宽带")   → id=1
2. time_options()                      → latest=2026-07
3. metric_series(metric_ids=[1], zone_ids=[99], gran=2,
                 start_month="2026-01", end_month="2026-07")
```

实测：`row_count=7`，首行 `2026-01-01 → 112,982,266`，末行 `2026-07-01 → 114,124,518`。

回答要点：直接给 7 行表格；若要"净增"，自行用首末行相减并说明是**自行计算**（不是工具给的）。

---

## 剧本 C：环比 / 同比（注意 A/B 陷阱）

> 用户：「7 月家庭宽带用户数环比 6 月怎么样？」

```
metric_compare(metric_ids=[1], zone_ids=[99],
               month_a="2026-07",    ← 本期放 A
               month_b="2026-06")    ← 对比期放 B
```

实测返回：

```json
{"rows":[{"value_a":114124518.0,"value_b":113959639.0,
          "delta":164879.0,"delta_pct":0.1447}]}
```

✅ 正确表述：`环比增加 164,879 户（+0.14%）`。

❌ 若把 A/B 传反（`month_a="2026-06"`），会得到 `delta=-164879`，
会被误读成"下降"。**符号对不上就先检查 A/B，不要怀疑工具。**

同比同理：`month_a="2026-07", month_b="2025-07"`。

---

## 剧本 D：省份排名（Top / Bottom N）

> 用户：「7 月家庭宽带用户数最多的 5 个省？」

```
1. metric_search(keyword="家庭宽带")  → id=1
2. metric_series(metric_ids=[1], level=1, gran=2, month="2026-07")   ← 一次拿全 32 省
3. 自行按 metric_value 降序排序，取前 5
```

实测（降序前 3）：`内蒙古 6,207,244` / `河北 5,947,880` / `西藏 5,708,312`；
（升序前 2）：`安徽 1,469,131` / `台湾 1,610,933`。

**不要用 `zone_names=["广东","浙江"]` 做多省对比**——那是 AND 语义，返回 0 行。
要么用 `level=1` 全拿，要么用 `zone_ids=[106,108]`。

排序在模型侧完成：工具没有 `order_by` 参数。

---

## 剧本 E：占比指标横向对比

> 用户：「各省千兆用户占比排个序？」

```
1. metric_search(keyword="千兆")        → 区分"千兆用户占比"(id=5) 与 "1000M及以上宽带用户数"(id=4)
2. metric_series(metric_ids=[5], level=1, gran=2, month="2026-07")
3. 降序排序
```

实测 top3：`湖北 0.796371` / `青海 0.784521` / `辽宁 0.731357`。

回答要点：**`metric_value` 是 0–1 小数，要 ×100 说成 79.6%**，不要直接写 "0.796%"。

---

## 剧本 F：多指标组合看结构

> 用户：「FTTR 发展得怎么样？用户数和占比。」

```
1. metric_search(keyword="FTTR")   → 命中 20 条，取 id=10(FTTR用户数)、11(FTTR用户占比)
2. metric_series(metric_ids=[10,11], zone_ids=[99], gran=2,
                 start_month="2025-07", end_month="2026-07")
```

实测：`row_count=26`（2 指标 × 13 月）。2026-07：用户数 `121,447,370`、
占比 `0.455318`（→ 45.5%）。

回答要点：用户数用千分位；占比换算成百分比；两者分别陈述，不要算"除以总数"——
演示数据里它不成立（见 SKILL.md §3 第 4 条）。

---

## 剧本 G：模板覆盖不了的复杂查询（逃生口）

> 用户：「7 月各省千兆占比与 FTTR 占比，按两者差值排序，只要差距最大的 5 个省。」

模板一次只能查一个指标、无法跨指标计算，需要 SQL：

```
1. table_schema()      ← 必做，确认真实列名（字典表只有 id / metric_name_cn）
2. run_readonly_sql(sql="""
     SELECT z.zone_name,
            MAX(CASE WHEN v.dictionary_id=5  THEN v.metric_value END) AS qianzhao_pct,
            MAX(CASE WHEN v.dictionary_id=11 THEN v.metric_value END) AS fttr_pct,
            MAX(CASE WHEN v.dictionary_id=5  THEN v.metric_value END)
              - MAX(CASE WHEN v.dictionary_id=11 THEN v.metric_value END) AS gap
     FROM ads_jk_business_metric_values v
     JOIN d_zone_info z ON z.zone_id = v.zone_id
     WHERE v.dictionary_id IN (5, 11)
       AND v.time_granularity = 2
       AND v.data_time = '2026-07-01'
       AND z.zone_level = 1
     GROUP BY z.zone_name
     ORDER BY gap DESC
     LIMIT 5
   """)
```

要点：

- `data_time` 是**每月 1 日**字符串，写 `'2026-07-01'`；
- 务必带 `LIMIT`（否则自动补 1000 行，可能被预算裁剪）；
- 先跑小 `LIMIT` 验证列名与口径，再放开；
- 若返回 `rejected: true`，读 `rejected_reason` 按规则改（多语句/写操作/白名单/解析失败）。

---

## 剧本 H：出图

> 用户：「把全国家庭宽带用户数今年走势画个折线图。」

```
1. metric_series(metric_ids=[1], zone_ids=[99], gran=2,
                 start_month="2026-01", end_month="2026-07")
   → 拿到 7 行 rows
2. 整理：
   categories = ["2026-01", ..., "2026-07"]      ← 从 data_time 取 YYYY-MM
   series     = [{"name":"家庭宽带用户数", "values":[112982266, ..., 114124518]}]
3. chart_spec(type="line", title="全国家庭宽带用户数（2026 年）",
              categories=categories, series=series, unit="户")
```

要点：

- `values` 长度必须与 `categories` 完全一致（7 = 7）；
- 数据有缺月时对应位置填 `null`，**不要填 0**；
- 成功后前端直接渲染图表，**正文只写文字解读，不要再贴 option JSON**。

---

## 剧本 I：先探底再回答（用户问题模糊时）

> 用户：「家客业务现在什么情况？」

不要一上来就查数。按顺序：

```
1. dimensions()      → 粒度、区域层级、表结构、zone_count
2. time_options()    → 数据到哪个月
3. metric_list(limit=50)  或  按业务方向用 metric_search 分批探查
4. 用 1-2 个核心指标（id=1 家庭宽带用户数 + id=5 千兆用户占比）给一个概览
5. 反问用户具体想看哪个方向
```

要点：模糊问题给"最小可信概览 + 明确的反问"，不要堆 20 个指标。

---

## 剧本 J：MCP 未连接 / 工具缺失时

若本轮工具列表里**没有** `metric_search` / `metric_series` 等 sqlbot 工具：

- ✅ 如实告诉用户：「本轮 sqlbot MCP 未连接，无法查询家客数据」，并说明可在设置里检查连接状态；
- ❌ **不要**用 `code_run` 手写 SQL 去连数据库；
- ❌ **不要**用 `assets/metric-catalog.csv` 的静态快照当"当前数据"回答数值问题
  （它只有 id 和名称，没有指标值）；
- ❌ **不要**编造数值。

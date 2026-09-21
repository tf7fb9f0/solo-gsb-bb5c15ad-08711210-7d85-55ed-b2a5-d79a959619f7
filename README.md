# 乐谱安全翻页规划 API

提交按演奏顺序排列的谱面系统高度、相邻系统边界的休止秒数、页面可用高度与最低安全休止，
服务用动态规划生成**完整分页方案**：

- 每页容纳连续若干系统，总高度**不得超过页面可用高度**；
- 支持两种排版模式（`layout_mode`）：
  - `single`（默认）：单页逐页翻，只允许在休止秒数 **≥ 最低安全休止**的边界翻页
    （最后一页之后无需翻页）；
  - `spread`：双页谱架，相邻左、右页组成一组同时可见，**组内（左页→右页）边界不检查休止**；
    只有**翻过右页**进入下一组才是实体翻页，要求休止 ≥ 最低安全休止，
    且只有实体翻页参与最短翻页休止最大化；
- 支持人工断点约束：`forced_break_after` 指定的系统之后**必须分页**，
  `forbidden_break_after` 指定的系统之后**禁止分页**；
- 可选**实体翻页恢复约束**：提供 `boundary_times`（各边界的演奏秒数，严格递增）
  与 `min_turn_gap_seconds`（默认 `0`）后，任意两次**相邻实体翻页**的时间差
  必须至少为该秒数。`single` 模式每次翻页都计入；`spread` 模式仅**右页之后**
  的翻页计入，**组内（左页→右页）换页不消耗恢复时间**；第一次实体翻页不检查间隔；
- 优化目标按字典序依次进行：
  1. **最大化**全曲最短的一次（实体）翻页休止（瓶颈值，越大越安全）；
  2. 在达到该瓶颈的方案中，**最小化各页留白高度的平方和**；
  3. 仍并列时，**取页数更少**的方案。

无解时不保存任何部分方案，并在响应中指出**首个无法容纳的系统**、**首个无法安全完成的
（实体）翻页边界**，或**首个实体翻页恢复间隔不可达的边界及其前次翻页边界**。
所有成功方案存入 SQLite，可随时查询（含新增字段的完整回放）。

## 快速启动

唯一需要的一条命令（依赖安装与建库建表均随启动自动完成）：

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**

- 交互式接口文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 配置

通过环境变量配置（`docker-compose.yml` 中已给出默认值，均可不改）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/plans.db`（容器内） | SQLite 数据库文件路径，Compose 已挂载卷 `plans-data` 持久化 |
| `PORT` | `8000` | 服务监听端口，Compose 映射到宿主机 8000 |

## 接口

### `POST /plans` — 生成并保存分页方案

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "system_heights": [120, 130, 110, 125, 115],
    "boundary_rests": [1.0, 4.0, 0.5, 6.0],
    "page_height": 300,
    "min_safe_rest": 2.0
  }'
```

请求参数：

- `system_heights`：按顺序排列的各谱面系统高度（正数，至少 1 个）；
- `boundary_rests`：相邻系统之间边界的休止秒数，长度 = 系统数 − 1（非负）；
- `page_height`：页面可用高度（正数）；
- `min_safe_rest`：最低安全休止秒数（非负）；
- `layout_mode`：排版模式，`single`（默认）或 `spread`；
- `spread_start_side`：`left`（默认）或 `right`，spread 模式下第 1 页所在的一侧，
  之后左右交替；single 模式下忽略；
- `forced_break_after`：人工强制分页点（系统下标数组，默认 `[]`）——对应系统之后
  **必须**分页；
- `forbidden_break_after`：人工禁止分页点（系统下标数组，默认 `[]`）——对应系统之后
  **不得**分页；
- `boundary_times`：相邻系统之间边界的**演奏秒数**（可选，长度 = 系统数 − 1，
  非负且**严格递增**）；
- `min_turn_gap_seconds`：相邻两次实体翻页之间的最小恢复间隔秒数（可选，默认 `0`，
  即不启用）。设为正数时**必须**同时提供 `boundary_times`。

两组断点约束的下标规则（违反时返回 `422` 并给出具体冲突）：

- 下标指向末系统（其后无需分页）或越界 → `422`；
- 同一系统同时出现在两组中 → `422`，错误信息列出冲突下标。

成功返回 `201`（single 模式示例）：

```json
{
  "id": 1,
  "layout_mode": "single",
  "spread_start_side": "left",
  "page_count": 3,
  "min_turn_rest": 4.0,
  "total_slack_squared": 40950.0,
  "pages": [
    {"page": 1, "start_system": 0, "end_system": 1, "used_height": 250.0, "slack": 50.0, "turn_rest_after": 4.0, "side": null, "physical_turn_after": true},
    {"page": 2, "start_system": 2, "end_system": 3, "used_height": 235.0, "slack": 65.0, "turn_rest_after": 6.0, "side": null, "physical_turn_after": true},
    {"page": 3, "start_system": 4, "end_system": 4, "used_height": 115.0, "slack": 185.0, "turn_rest_after": null, "side": null, "physical_turn_after": false}
  ]
}
```

每页字段：

- `turn_rest_after`：该页之后（实体）翻页处的休止秒数；最后一页、以及 spread 组内
  （左页→右页）无需翻页的边界为 `null`；
- `side`：spread 模式下为 `"left"` / `"right"`；single 模式不分左右页，为 `null`；
- `physical_turn_after`：该页之后是否需要实体翻页。single 模式每次翻页都是实体翻页
  （最后一页除外）；spread 模式只有右页之后才是实体翻页。

`min_turn_rest` 为全曲最短（实体）翻页休止（无需实体翻页时为 `null`）。

#### spread 双页谱架示例

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "system_heights": [6, 6, 6],
    "boundary_rests": [0.2, 8.0],
    "page_height": 10,
    "min_safe_rest": 1.0,
    "layout_mode": "spread",
    "spread_start_side": "left"
  }'
```

边界 0 的休止 0.2s 低于最低安全休止，single 模式下无解；spread 模式下它位于
组内（页 1 左 → 页 2 右），不检查休止，只有页 2（右页）之后的实体翻页需要达标：

```json
{
  "layout_mode": "spread",
  "spread_start_side": "left",
  "page_count": 3,
  "min_turn_rest": 8.0,
  "pages": [
    {"page": 1, "start_system": 0, "end_system": 0, "side": "left",  "physical_turn_after": false, "turn_rest_after": null, "...": "..."},
    {"page": 2, "start_system": 1, "end_system": 1, "side": "right", "physical_turn_after": true,  "turn_rest_after": 8.0,  "...": "..."},
    {"page": 3, "start_system": 2, "end_system": 2, "side": "left",  "physical_turn_after": false, "turn_rest_after": null, "...": "..."}
  ]
}
```

#### 人工断点约束示例

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "system_heights": [100, 100, 100, 100],
    "boundary_rests": [5.0, 5.0, 5.0],
    "page_height": 200,
    "min_safe_rest": 1.0,
    "forced_break_after": [1],
    "forbidden_break_after": [2]
  }'
```

系统 1 之后必须分页、系统 2 之后不得分页，唯一的分页点落在系统 1 之后；
成功响应与后续 `GET /plans/{id}` 回放都会保留这两组约束：

```json
{
  "forced_break_after": [1],
  "forbidden_break_after": [2],
  "page_count": 2,
  "min_turn_rest": 5.0,
  "pages": [
    {"page": 1, "start_system": 0, "end_system": 1, "turn_rest_after": 5.0, "...": "..."},
    {"page": 2, "start_system": 2, "end_system": 3, "turn_rest_after": null, "...": "..."}
  ]
}
```

约束语义：

- 约束在动态规划转移阶段生效：禁止断点裁掉对应的分页边，强制断点要求页边界
  必须落在该系统之后（任何页面不得同时包含该系统与下一系统）；
- 强制分页只约束页边界位置，**不改变实体翻页判定**：spread 组内（左页之后）的
  强制断点仍不检查休止、不参与瓶颈；只有右页之后的断点才检查 `min_safe_rest`
  并参与瓶颈优化；
- 约束导致无可行方案时沿用现有的 `422` 无解诊断且**不保存任何部分方案**；
  若首个不可达的边界正是被禁止分页的边界，诊断中会带 `forbidden_break: true`，
  若该边界同时是强制分页点则带 `forced_break: true`。

断点冲突（同一系统既强制又禁止，或指向末系统）返回 `422`，并给出具体冲突：

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body"],
      "msg": "Value error, forced_break_after 与 forbidden_break_after 存在冲突下标: [2]"
    }
  ]
}
```

#### 实体翻页恢复约束示例

演奏者翻过一页后需要时间恢复，两次实体翻页不能离得太近。`boundary_times` 给出每个
边界在乐曲中的演奏秒数，`min_turn_gap_seconds` 给出相邻两次实体翻页的最小间隔：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "system_heights": [5, 5, 5, 5],
    "boundary_rests": [9.0, 9.0, 9.0],
    "page_height": 10,
    "min_safe_rest": 1.0,
    "boundary_times": [10.0, 12.0, 30.0],
    "min_turn_gap_seconds": 5.0
  }'
```

边界 0（第 10s）与边界 1（第 12s）仅相差 2s，不允许在两处连续翻页；规划器因此把
系统 0–1、2–3 各合为一页，整曲只在边界 1 翻页一次（第一次实体翻页不检查间隔）：

```json
{
  "boundary_times": [10.0, 12.0, 30.0],
  "min_turn_gap_seconds": 5.0,
  "page_count": 2,
  "min_turn_rest": 9.0,
  "pages": [
    {"page": 1, "start_system": 0, "end_system": 1, "turn_rest_after": 9.0, "physical_turn_after": true,  "...": "..."},
    {"page": 2, "start_system": 2, "end_system": 3, "turn_rest_after": null,  "physical_turn_after": false, "...": "..."}
  ]
}
```

`spread` 模式下组内换页（左页→右页）**不消耗恢复时间**，只有右页之后的实体翻页
计入间隔并更新“上一次实体翻页”。恢复约束在两阶段 DP 中作为额外的分页边裁剪条件，
**不改变优化顺序**：仍然先最大化瓶颈休止、再最小化留白平方和、最后减少页数——
更优瓶颈若需要两次过近的实体翻页，会被恢复约束裁掉并退而求其次。

恢复约束的参数校验（违反时返回 `422`）：

- `min_turn_gap_seconds` 为负或非有限数值 → `422`；
- 设为正数但未提供 `boundary_times` → `422`；
- `boundary_times` 长度不等于系统数 − 1、含负数/非有限数值、或不满足严格递增 → `422`；
- 仅提供 `boundary_times` 而保持 `min_turn_gap_seconds = 0` 合法，约束不启用。

当任何分页方式都无法满足恢复间隔时，返回 `422`，指出**首个恢复间隔不可达的边界**
以及可达方案中**前次实体翻页的边界**（间隔最大仍不足者），且不落库：

```json
{
  "detail": {
    "error": "no_feasible_pagination",
    "reason": "turn_gap_unreachable",
    "boundary_index": 1,
    "between_systems": [1, 2],
    "rest_seconds": 9.0,
    "min_safe_rest": 1.0,
    "prev_turn_boundary_index": 0,
    "prev_turn_between_systems": [0, 1],
    "turn_time_seconds": 12.0,
    "prev_turn_time_seconds": 10.0,
    "gap_seconds": 2.0,
    "min_turn_gap_seconds": 5.0,
    "physical_turn": true,
    "message": "边界 1 是首个实体翻页恢复间隔不可达的边界：距上一次实体翻页（边界 0，10.0s）仅 2.0s，低于要求的最小恢复间隔 5.0s"
  }
}
```

无解返回 `422`，指出首个问题区间，且**不保存任何部分方案**：

```json
{
  "detail": {
    "error": "no_feasible_pagination",
    "reason": "unsafe_boundary",
    "boundary_index": 1,
    "between_systems": [1, 2],
    "rest_seconds": 0.2,
    "min_safe_rest": 1.0,
    "physical_turn": true,
    "message": "边界 1（系统 1 与系统 2 之间）是首个无法安全完成的实体翻页边界：此处必然结束一页右页、需要实体翻页，但休止 0.2s 低于最低安全休止 1.0s"
  }
}
```

`reason` 取值：

- `system_exceeds_page_height`：首个高度超过页面可用高度的系统（附 `system_index`），任何页面都无法容纳；
- `unsafe_boundary`：首个无法安全断开的边界（附 `boundary_index` 与相邻系统下标）。
  spread 模式下该边界必为实体翻页边界（附 `physical_turn: true`）——若该页能落在左页，
  组内边界本无需检查休止。若该边界被 `forbidden_break_after` 禁止分页，
  诊断附 `forbidden_break: true`；若该边界同时是强制分页点，附 `forced_break: true`；
- `turn_gap_unreachable`：首个实体翻页恢复间隔不可达的边界。附
  `prev_turn_boundary_index` / `prev_turn_between_systems`（前次实体翻页边界）、
  `turn_time_seconds` / `prev_turn_time_seconds`（两次翻页的演奏秒数）、
  `gap_seconds`（实际间隔）与 `min_turn_gap_seconds`（要求间隔）；强制分页点附
  `forced_break: true`。同一边界休止不足时优先报告 `unsafe_boundary`。

### `GET /plans/{id}` — 查询完整方案

```bash
curl http://localhost:8000/plans/1
```

不存在时返回 `404`。响应完整回放保存时的请求与结果，包括 `layout_mode`、
`spread_start_side`、`forced_break_after`、`forbidden_break_after`、
`boundary_times`、`min_turn_gap_seconds` 及逐页的 `side`、`physical_turn_after`。
旧版本保存的记录缺少新增字段时按默认值（`null` / `[]` / `0.0`）回放。

### `GET /plans` — 列出方案摘要

```bash
curl 'http://localhost:8000/plans?limit=50&offset=0'
```

摘要含 `layout_mode`，便于区分两种排版模式的方案。

## 本地开发（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试（需额外安装 `pytest` 与 `httpx`）：

```bash
python3 -m pytest tests/ -q
```

## 实现说明

- 规划核心为两阶段精确动态规划（`app/planner.py`）：
  1. widest-path DP 求最大可行瓶颈（最短实体翻页休止的最大值），同时得到各位置可达性；
  2. 仅保留休止 ≥ 瓶颈的实体翻页边，做（留白平方和, 页数）字典序最小的 DP 并回溯切分点。
- spread 模式下 DP 状态额外携带**已用页数奇偶**：第 1 页位于 `spread_start_side`，
  之后左右交替，页码奇偶因此决定该页之后是否为实体翻页（右页之后才是）。
  组内（左页→右页）边界不参与休止检查与瓶颈计算。启用恢复约束时状态再携带
  **上一次实体翻页所在边界**（`-1` 表示尚无）：组内换页原样传递、不消耗恢复时间，
  右页后的实体翻页要求距上一次实体翻页至少 `min_turn_gap_seconds` 秒并更新该状态。
- 人工断点在两阶段 DP 的转移阶段统一生效：禁止断点直接跳过对应的分页边；
  强制断点通过预计算的"最早起始位置"裁剪所有跨过该断点的页面。强制分页不改变
  实体翻页判定——spread 组内（左页之后）的强制断点仍不检查休止、不参与瓶颈。
- 无解诊断：先检查首个超高的系统；否则取首个不可达位置（spread 模式指两种页数奇偶
  均不可达）——可证明此时其左侧边界要么被 `forbidden_break_after` 禁止分页，
  要么是无法安全完成的（实体）翻页边界（休止不足或恢复间隔不可达）。恢复间隔
  不可达时同时给出可达方案中前次实体翻页的边界（最早、间隔最大仍不足者）。
- 存储使用 SQLite（`app/storage.py`），请求与结果以 JSON 整体保存，
  只有成功生成的完整方案才会入库；新增字段随 JSON 自动持久化并可回放。

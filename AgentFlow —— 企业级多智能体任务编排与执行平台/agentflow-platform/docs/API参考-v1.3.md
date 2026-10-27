# API 参考 · v1.3 增补

> 面向 v1.3 新增的 **任务归档（Archive）** 能力：一个接口（`POST /tasks/{id}/archive`）、
> 一个列表参数（`GET /tasks?archived=`）、一条看板口径约定。
> 基础接口（鉴权 / 任务 / 审批 / 记忆 / 导出 / 知识库自检）见 [`API参考-v1.1.md`](API参考-v1.1.md)；
> 工具注册表 / 全局限流 / 评审批注见 [`API参考-v1.2.md`](API参考-v1.2.md)。
> 本文所有示例均为**真实响应**（本机 uvicorn → MySQL 3307 全链路跑出，51 条真实任务数据）。

统一约定（与 v1.0 一致）：
- 成功判据是 `code === "OK"`，不是 HTTP 200；
- 响应信封 `{code, message, data, trace_id}`；
- 鉴权 `Authorization: Bearer <access_token>`；SSE 额外支持 `?access_token=`。

---

## 0. 设计定位：归档不是删除

这一条必须在读接口之前先明确，否则很容易把归档当成"半成品删除"来用。

| 动作 | 数据行 | 列表（默认） | 详情页 | 子任务 / 审批单 / 轨迹 / 产物 | 可逆 |
|---|---|:---:|:---:|:---:|:---:|
| **归档** `archived_at` | 保留 | 隐藏 | **仍可打开** | 全部保留，不做任何清理 | 随时取消，一键恢复 |
| **软删除**（第二阶段，未实现） | 保留（`deleted_at`） | 隐藏 | 404 | 级联处理（清 Redis checkpoint / 图表文件） | 需管理员恢复 |

归档解决的是**列表整洁度**，不是**数据留存**。典型场景：跑完的 51 条任务里，
有 40 条是调试产物，用户希望"别让它占着我的第一屏"，但审计时还要能翻回去。

> 因此**归档阶段完全不动读路径语义**：`GET /tasks/{id}`、导出、轨迹查询对已归档任务
> 行为与归档前完全一致。

---

## 1. `POST /api/v1/tasks/{task_id}/archive` — 归档 / 取消归档

### 1.1 权限（刻意与详情接口不对齐）

所需角色：**ADMIN / APPROVER**（VIEWER 403）。

**权限口径（重要，请勿"顺手统一"）：**

> 详情页仍保持仅创建人可见（防止越权读取隐私），但归档操作赋予 ADMIN 全局治理权。

展开说明：

| 接口 | 权限 | 为什么不同 |
|---|---|---|
| `GET /tasks/{id}`（详情） | **ADMIN 也仅限本人创建**，他人任务 403 | 详情含 goal 原文、工具入参、检索片段——这是**读隐私**。管理员不该能随手翻别人的业务问题 |
| `POST /tasks/{id}/archive` | **ADMIN / APPROVER 不限创建人** | 管理员在「全部任务」列表里能看到所有人的任务；若按钮点下去 403，这个能力就是摆设。归档**不读正文、不改数据、可随时取消**，最坏后果仅是"从默认列表里收起来" |
| `POST /tasks/{id}/archive`（VIEWER） | 403 | 访客只能看已完成任务，没有"整理列表"的语义 |

这条不对齐是**有意设计**，不是漏改。若未来有人想把两者统一，请先想清楚：
统一到"仅本人"→ 管理员的治理能力失效；统一到"不限本人"→ 越权读取隐私。

代码位置：`app/routers/task_archive.py` 模块 docstring「约束」段 + `archive_task()` 内联注释。

### 1.2 请求

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"archived": true}' \
  http://127.0.0.1:8000/api/v1/tasks/51/archive
```

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `archived` | bool | `true` | `true` 归档；`false` 取消归档 |

请求体可整体省略（视为归档）。**幂等**：目标状态已满足时返回 `changed=false`，
不报错、不改时间戳。

### 1.3 归档成功（真实响应）

```json
{
  "code": "OK",
  "message": "ok",
  "data": {
    "task_id": 51,
    "task_no": "AF20261026-0001",
    "status": "COMPLETED",
    "archived": true,
    "archived_at": "2026-10-26T03:17:20",
    "changed": true
  },
  "trace_id": "fcd30d5b691447739584ed580ade5705"
}
```

### 1.4 幂等重放（同一请求再发一次）

```json
{
  "code": "OK",
  "message": "ok",
  "data": {
    "task_id": 51,
    "task_no": "AF20261026-0001",
    "status": "COMPLETED",
    "archived": true,
    "archived_at": "2026-10-26T03:17:20",
    "changed": false
  },
  "trace_id": "50d4841cddce434c9b2dae175bf97d47"
}
```

`archived_at` 与首次一致 —— 幂等重放不会刷新时间戳（否则"归档时间"会变成"最后一次误点时间"）。

### 1.5 取消归档

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"archived": false}' http://127.0.0.1:8000/api/v1/tasks/51/archive
```

```json
{
  "code": "OK",
  "message": "ok",
  "data": {
    "task_id": 51,
    "task_no": "AF20261026-0001",
    "status": "COMPLETED",
    "archived": false,
    "archived_at": null,
    "changed": true
  },
  "trace_id": "9157f6c64ea84bc3b5daa316729b8aca"
}
```

### 1.6 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` / `task_no` | int / str | 任务标识 |
| `status` | str | 归档后的任务状态（归档不改状态） |
| `archived` | bool | 归档操作**之后**的状态，前端以此为准刷新按钮文案 |
| `archived_at` | str \| null | ISO8601（UTC naive，无时区后缀）；`null` = 未归档 |
| `changed` | bool | 本次是否真的改了库。`false` = 幂等空转，前端无需重新拉列表 |

### 1.7 错误响应（全部为真实响应）

**非终态任务 409** —— 只有 `COMPLETED` / `FAILED` / `CANCELLED` 可归档：

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"archived":true}' http://127.0.0.1:8000/api/v1/tasks/43/archive   # task 43 = PLANNING
```

```json
{
  "code": "TASK_STATE_CONFLICT",
  "message": "任务当前状态为 PLANNING，只有终态（已完成 / 失败 / 已终止）才能归档；如需清理请先终止任务",
  "data": null,
  "trace_id": "d9a54f99614149c8a5b849e6681a7e5b"
}
```

> 为什么禁止归档运行中任务：`archived_at` 只影响列表可见性，不会停掉后台 graph。
> 若允许，用户会看到"任务从列表消失了但还在烧 token"，无法解释也无法召回。

**任务不存在 404**：

```json
{"code": "NOT_FOUND", "message": "任务 999999 不存在", "data": null, "trace_id": "..."}
```

**VIEWER 越权 403**：

```json
{
  "code": "FORBIDDEN",
  "message": "当前角色无权执行该操作",
  "data": {"detail": {"required_roles": ["admin", "approver"], "actual_role": "viewer"}},
  "trace_id": "64a44a1c8489433c825f77a49f0bf996"
}
```

---

## 2. `GET /api/v1/tasks?archived=` — 列表归档口径

在 v1.1 的任务列表上新增一个可选查询参数，**服务端过滤**（不是前端 filter，
保证 `meta.total` 与分页对得上）。

| 取值 | 语义 | 典型用途 |
|---|---|---|
| 省略 / `null` | **默认口径**：只返回**未归档** | 工作台首屏 |
| `archived=true` | 只返回**已归档** | 顶部「查看归档任务」 |
| `archived=false` | 只返回**未归档**（与省略等价） | 显式写法，便于前端统一拼参 |

### 2.1 真实数据（51 条任务，其中 1 条已归档）

```bash
# 默认：50 条未归档中的前 5 条 —— 注意列表项已带 archived_at 字段
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/tasks?page=1&size=5"
```

```json
{"page": 1, "size": 5, "total": 50}
  AF20261025-0025 COMPLETED archived_at= null
  AF20261025-0024 COMPLETED archived_at= null
  ...
```

```bash
# 只看已归档
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/tasks?page=1&size=5&archived=true"
```

```json
{"page": 1, "size": 5, "total": 1}
  AF20261026-0001 COMPLETED archived_at= 2026-10-26T03:17:20
```

**对账关系**：`archived 省略的 total` + `archived=true 的 total` = 该用户可见的任务总数。
（上例 50 + 1 = 51。）写测试时这是最容易踩的断言点——
ADMIN 的列表不限创建人，别用"我自己创建了几条"去算期望值。

### 2.2 列表项新增字段

`TaskListItem` 新增：

| 字段 | 类型 | 说明 |
|---|---|---|
| `archived_at` | str \| null | 归档时间；`null` 表示未归档。前端用它渲染「已归档」标签与按钮文案 |

### 2.3 前端交互契约

工作台 `DashboardView.vue`：

- 标题区 `a-radio-group` 切换「全部任务 / 已归档」，切换时重置到第 1 页；
- 只有 `auth.isAdmin || auth.isApprover` 且 `status ∈ {COMPLETED, FAILED, CANCELLED}` 时才渲染操作列的
  「归档 / 取消归档」链接（`canArchive()`）；
- 按钮文案由 `record.archived_at` 决定：非空 → 「取消归档」，为空 → 「归档」；
- 点击弹 `Modal.confirm`，确认后行级 loading，成功后重新拉列表（由服务端过滤决定该行是否还留在当前视图）。

---

## 3. 看板统计口径：**包含已归档**

`GET /dashboards/summary` / `/tools` / `/failure-reasons` / `/trend` 的聚合**不做**
`archived_at IS NULL` 过滤，已归档任务照常计入。

这条是明确决策，不是遗漏。理由：

1. **列表是工作视图，大盘是事实视图。** 归档只影响"我第一屏想看什么"，
   而任务确实跑过、确实烧了 7805 token、确实产出了图表。
2. **抠掉会造成不可解释的漂移。** 同一时间段，完成率 / 平均耗时 / token 总量会因为
   "谁顺手点了归档"而变化，这种数据没人敢用。
3. 若未来要做"活跃任务口径"，应以**显式参数**（如 `include_archived=false`）开启，
   而不是改默认值。

真实验证（2026-10-26 窗口内只有 1 条任务 task#51，把它归档后）：

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/dashboards/summary?start=2026-10-26T00:00:00&end=2026-10-27T00:00:00"
```

```json
{
  "task_total": 1,
  "status_breakdown": {"PLANNING": 0, "RUNNING": 0, "AWAITING_APPROVAL": 0,
                       "COMPLETED": 1, "FAILED": 0, "CANCELLED": 0}
}
```

归档前后 `task_total` 恒为 1 —— 预期行为。

**唯一的例外是第二阶段「软删除（`deleted_at`）」**：那是用户明确宣告"这份数据无效"，
届时**所有**读路径（含 4 处聚合、列表、详情、导出、审批收件箱、评测）统一加
`deleted_at IS NULL`，一次性对齐，不在归档阶段提前埋半套过滤。

代码位置：`app/routers/dashboards.py` 模块 docstring「归档口径」段 + `_load_summary()` 内联注释。

---

## 4. 数据库变更

`tasks` 表新增一列（无新表、无外键、无级联）：

```sql
ALTER TABLE tasks ADD COLUMN archived_at DATETIME NULL COMMENT '归档时间；NULL=未归档';
CREATE INDEX ix_tasks_archived_at ON tasks (archived_at);
```

迁移脚本（幂等，支持 MySQL 与 SQLite，支持 `--dry-run`）：

```bash
.venv/Scripts/python.exe scripts/migrate_add_task_archive.py --dry-run
.venv/Scripts/python.exe scripts/migrate_add_task_archive.py
.venv/Scripts/python.exe scripts/migrate_add_task_archive.py --sqlite storage/_dev_serve.db
```

> 项目没有 alembic 版本目录，`Base.metadata.create_all` **只补表不补列**。
> 换库 / 重建库后必须手工执行本脚本，否则 `tasks.archived_at` 缺失，
> 列表查询会抛 `Unknown column`。同一坑位历史上已踩过一次（`tasks.result_payload`）。

---

## 5. 错误码速查（v1.3 新增部分）

| code | HTTP | 场景 |
|---|---:|---|
| `TASK_STATE_CONFLICT` | 409 | 对非终态任务（PLANNING / RUNNING / AWAITING_APPROVAL / SUMMARIZING）发起归档 |
| `NOT_FOUND` | 404 | `task_id` 不存在 |
| `FORBIDDEN` | 403 | VIEWER 调用归档接口 |

`ErrorCode.TASK_STATE_CONFLICT` 已在 `app/core/exceptions.py` 注册映射，
日志埋点：`task_archived` / `task_unarchived` / `task_archive_noop`（三者都带 `operator`）。

---

## 6. 测试覆盖

`tests/test_v13_task_archive.py`（13 例）：

1. 归档终态任务 → `changed=true`，`archived_at` 非空；
2. 取消归档 → `archived_at` 归 `null`；
3. 幂等重放 → `changed=false` 且时间戳不变；
4. 非终态归档 → 409 `TASK_STATE_CONFLICT`；
5. ADMIN / APPROVER 均可归档**任意**终态任务（全局治理口径）；
6. VIEWER → 403；
7. 列表默认排除已归档；`archived=true` 只含已归档；三态 `total` 互相对账；
8. 归档**不删除**数据行：直接查库断言行仍在、状态未变；
9. 不存在的任务 → 404。

---

## 7. 第二阶段预告：软删除（未实现，勿前提依赖）

归档落地后再评估。已知需要处理的点（此处仅存档，接口未开放）：

- `tasks.deleted_at` / `deleted_by`，软删除需输入 `task_no` 二次确认；
- 级联：清 Redis checkpoint（`checkpoint:*`）+ 关闭 SSE 流 + 解除 `memory.task_id` 关联；
- **7 处读路径**统一加 `deleted_at IS NULL`：任务列表、详情、看板 4 处聚合、
  评测批次、审批收件箱、导出；
- 软删除本身也排除非终态任务（同样 409）。

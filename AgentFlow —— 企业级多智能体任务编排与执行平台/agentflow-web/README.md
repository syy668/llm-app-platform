# agentflow-web

AgentFlow 企业级多智能体任务编排与执行平台 — 前端工程。

技术栈：**Vite 5 + Vue 3.5 + TypeScript + Arco Design Vue + AntV X6 + Pinia + axios**。

设计依据：《AgentFlow前端UI原型设计规范-v1.0.md》（布局、区块、交互、状态色板、跳转关系）。
接口依据：后端 `agentflow-platform/app/routers/*.py` 与 `app/schemas/*.py` 的实际定义，
**未新增任何后端不存在的接口或字段**；接口缺失处以 `【后端暂未提供该接口，仅预留UI占位】` 显式标注。

---

## 快速开始

```bash
# 1. 安装依赖（Node ≥ 18）
npm install

# 2. 配置后端地址
cp .env.example .env.development
#   编辑 VITE_API_BASE_URL / VITE_PROXY_TARGET 指向实际后端

# 3. 启动开发服务器（默认 http://127.0.0.1:5173）
npm run dev

# 4. 生产构建（产物在 dist/）
npm run build

# 5. 本地预览构建产物
npm run preview

# 6. 类型检查（CI 用；build 脚本本身不做类型检查，速度快）
npm run typecheck
```

### 环境变量（.env.example）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `VITE_API_BASE_URL` | 后端根地址（**不含** `/api/v1`） | `http://127.0.0.1:8000` |
| `VITE_API_PREFIX` | API 版本前缀，与后端 `settings.API_V1_PREFIX` 一致 | `/api/v1` |
| `VITE_PROXY_TARGET` | 开发期 Vite 代理目标（仅 `npm run dev` 生效） | `http://127.0.0.1:8000` |
| `VITE_REQUEST_TIMEOUT` | axios 超时（毫秒），SSE 不受此限制 | `20000` |
| `VITE_APP_TITLE` | 品牌名 | `AgentFlow` |
| `VITE_APP_VERSION` | 版本号（侧边栏展示） | `1.0.0` |

生产部署：把 `dist/` 交给 Nginx 托管即可。路由为 **hash 模式**，无需配置 history fallback；
若后端与前端不同域，需在网关放行 `Authorization` 头并允许 `text/event-stream` 长连接
（关闭代理缓冲：`proxy_buffering off`）。

---

## 目录结构

```
agentflow-web/
├── .env.example                 环境变量示例
├── .env.development             开发环境变量
├── index.html
├── package.json
├── tsconfig.json                TS 配置（应用代码）
├── tsconfig.node.json           TS 配置（vite.config.ts）
├── vite.config.ts               Vite 配置（别名 @ / 代理 / 分包）
└── src/
    ├── main.ts                  应用入口：Pinia / Router / Arco 全量注册
    ├── App.vue                  根组件：仅提供 Arco 中文语言环境
    ├── env.d.ts                 import.meta.env 强类型
    │
    ├── api/                     网络层（唯一出口）
    │   ├── http.ts              axios 实例：baseURL / 鉴权头 / 解包 code / 静默续期 / ApiError
    │   ├── auth.ts              POST /auth/login|refresh|logout、GET /auth/me
    │   ├── tasks.ts             /tasks 创建、详情、interrupt、resume、cancel、snapshot、trace
    │   ├── approvals.ts         GET /approvals、GET /approvals/{id}
    │   ├── memories.ts          /memories 增删查 + meta + recall
    │   ├── health.ts            /healthz、/readyz、/api/v1/health
    │   └── stream.ts            SSE 封装（EventSource + 令牌查询参数 + 终帧收尾 + 重连）
    │
    ├── types/                   与后端 Pydantic 模型一一对应的 interface
    │   ├── common.ts            ApiResponse / 分页 / ApiErrorShape
    │   ├── enums.ts             TaskStatus(7) / SubtaskStatus(7) / UserRole / ExecutionNode ...
    │   ├── auth.ts              LoginRequest / TokenPair / UserOut / SessionUser
    │   ├── task.ts              TimeRange / TaskCreateRequest / TaskDetail / Subtask / ArtifactItem
    │   ├── approval.ts          ApprovalInboxItem / ApprovalDetail / InterruptContext / ResumeRequest
    │   ├── trace.ts             TraceStep / CheckpointStep / TraceReplayData / SnapshotData
    │   ├── sse.ts               四类事件信封 + 各 phase 负载 + StreamHandlers
    │   ├── memory.ts            MemoryItem / MemoryListData / MemoryMetaData / RecallHit
    │   ├── health.ts            三个探针的返回结构
    │   └── dag.ts               NodeStatus（前端派生，非后端枚举）
    │
    ├── stores/
    │   ├── auth.ts              登录态 + 角色（唯一来源 GET /auth/me）
    │   └── approval.ts          待审批计数（GET /approvals 的 total）
    │
    ├── router/
    │   ├── index.ts             路由表 + 守卫（登录态 / 角色 / 标题）
    │   └── nav.ts               侧边栏导航配置（含角色可见性）
    │
    ├── layouts/
    │   └── BasicLayout.vue      侧边栏 + 顶栏 + 内容区
    │
    ├── components/              通用业务组件
    │   ├── DagGraph.vue         X6 只读 DAG 画布（节点状态色标 / 悬浮卡 / 自动布局）
    │   ├── TraceTimeline.vue    Trace 时间线（倍速回放 / 节点筛选 / 仅看异常 / payload 展开）
    │   ├── ApprovalDrawer.vue   审批抽屉（阻断原因 / 高危子任务 / payload / 意见）
    │   ├── JsonViewer.vue       JSON 预览（折叠 / 复制）
    │   ├── TaskStatusTag.vue    状态标签（任务 / 子任务 / 风险）
    │   └── StateBlock.vue       空 / 错误 / 加载 / **接口待接入** 四态占位
    │
    ├── views/
    │   ├── login/LoginView.vue              P01 登录（无角色选择）
    │   ├── dashboard/DashboardView.vue      P02 工作台大盘
    │   ├── task/TaskCreateView.vue          P03 新建任务
    │   ├── task/TaskDetailView.vue          P04 任务详情（DAG + 4 Tab + 审批抽屉）
    │   ├── approval/ApprovalInboxView.vue   P05 审批收件箱
    │   ├── eval/EvalView.vue                P06 评测与实验
    │   ├── memory/MemoryView.vue            P07 记忆管理
    │   └── system/SettingsView.vue          P09 系统设置
    │
    ├── utils/
    │   ├── format.ts            时间 / 耗时 / token / 倒计时格式化
    │   ├── status.ts            状态 → 文案 / 颜色 / 图标 唯一映射表
    │   └── recentTasks.ts       本机最近访问任务（弥补 GET /tasks 缺失）
    │
    └── styles/global.css        全局基础样式（极薄，外观交给 Arco 主题）
```

---

## 关键实现说明

### 1. 鉴权链路

```
POST /auth/login  →  TokenPair（access 2h / refresh 7d，**不含 role**）
        ↓ 写 storage（勾选"保持 7 天"→ localStorage，否则 sessionStorage）
GET  /auth/me     →  UserOut（role 唯一权威来源）
        ↓ 菜单裁剪 / 按钮置灰
路由守卫按 meta.roles 校验，越权时后端 403 兜底
```

- 401 + `AUTH_TOKEN_EXPIRED` → 拦截器用 refresh_token 静默续期并**重放一次**原请求；
  刷新做了单飞（single-flight），并发 401 只打一次 `/auth/refresh`。
- refresh_token 一次性消费，因此绝不能并发刷新。
- 登录页**没有任何角色选择控件**：`LoginRequest` 只有 `username` / `password`。

### 2. SSE 实时流

- `EventSource` 不能设置请求头，令牌走 `?access_token=`（后端 `sse_current_user` 明确支持）。
- `interrupt` / `complete` 是终帧，收到后主动 `close()`，否则浏览器会自动重连。
- 断线重连：浏览器原生携带 `Last-Event-ID`，同时额外传 `?last_event_id=` 兜底。
- **子任务状态不由 SSE 下发**，详情页在任务运行期每 4s 轮询 `GET /tasks/{id}` 补齐。

### 3. DAG 画布（X6）

- 节点 = 固定流水线（planner → human_gate? → subtasks → reviewer → summarizer → notifier?）
  + `subtasks[]`；边由 `depends_on`（存的是 **seq**）推导；`replan_count > 0` 时画虚线回边。
- 只读：`interacting` 全部关闭，仅保留平移与"适应画布"。
- 配色对齐规范：完成 `#00B42A` / 执行中 `#165DFF` / 等待 `#FF7D00` / 失败 `#F53F3F` / 待执行 `#C9CDD4`。

### 4. 接口缺口处理原则

后端未提供的接口**一律不造数据**，用 `StateBlock state="pending"` 明示并给出建议契约。
清单见 `docs/接口对接核对清单.md` 的「缺口汇总」一节。

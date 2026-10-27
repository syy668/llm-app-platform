# EvalAgent — RAG 自动化评测与 A/B 调优平台

独立 Vue3 前端工作台：不重新实现 RAG 问答、不重写 RAGAS 打分逻辑，**不新增任何后端服务**，全部通过 HTTP 调用上游【项目一：企业垂直知识库 Agent 后台】FastAPI 已有接口（`/api/v1/eval/*`、`/api/v1/auth/*`、`/api/v1/kbs`）。

## 1. 业务痛点

项目一已内置 RAGAS 评测底座（导入测试集、单次批量跑分、导出 CSV），但缺少：

- **A/B 对照实验**：无法对比两套 RAG 配置在同一数据集上的评测结果；
- **样本横向对比**：不能直接看同一个 question 在两套配置下的回答与分数差异，难以定位 BadCase 根因；
- **实验报告**：无法一键生成 A/B 对比 Markdown 报告留存调优记录。

EvalAgent 补齐「评测 → 对照 → 调优」闭环：实验编排、多任务对照、可视化对比、差异报告导出。

## 2. 技术栈

| 项 | 选型 |
| --- | --- |
| 框架 | Vue 3.5 + `<script setup lang="ts">` |
| 构建 | Vite 6 |
| UI | Element Plus 2.9（全量注册 + zh-cn） |
| 图表 | ECharts 5.6 |
| HTTP | Axios 1.7（JWT Bearer 拦截器） |
| 类型检查 | vue-tsc 2.2，`npm run typecheck` 0 errors |

## 3. 页面清单

| 路由 | 页面 | 说明 |
| --- | --- | --- |
| `/dataset` | 数据集管理 | CSV 上传（前端解析校验）、数据集列表、样本分页预览 |
| `/task` | 评测任务 | 创建任务表单、任务列表（进度条轮询）、详情抽屉、BadCase 筛选、导出 CSV |
| `/ab-compare` | A/B 实验看板（核心） | 双任务选择、同数据集校验、ECharts 指标柱状图、样本左右分栏对比（\|Δ\|>0.1 标黄）、Markdown 报告导出 |
| `/settings` | 系统设置 | 上游 API 地址本地配置、账号角色展示、备用 token 粘贴 |
| `/login` | 内嵌登录 | 直接调用项目一 `POST /auth/login`（两站点不同源无法共享 token 的兜底方案） |

公共组件：`AppSidebar.vue`（侧边导航）、`EmptyState.vue`（空态）、`BadCaseFilter.vue`（低分筛选）。

## 4. 启动命令

```bash
# 1. 安装依赖
npm install

# 2. 启动开发服务（端口 5174）
npm run dev
# 打开 http://localhost:5174

# 3. 类型检查（必须 0 errors）
npm run typecheck

# 4. 生产构建
npm run build
```

## 5. 依赖关系（必须先启动项目一）

```text
┌──────────────────────────┐
│ evalagent_vue（本项目）    │  http://localhost:5174
└────────────┬─────────────┘
             │ HTTP + JWT Bearer Token
             ▼
┌─────────────────────────────────────────┐
│ 项目一：企业垂直知识库 Agent 后台          │  http://127.0.0.1:8000
│  ├─ /api/v1/auth/login  /auth/me        │
│  ├─ /api/v1/kbs                         │
│  ├─ /api/v1/eval/datasets  /eval/runs   │
│  └─ /api/v1/eval/runs/{id}/scores|export│
└─────────────────────────────────────────┘
```

启动顺序：**项目一 FastAPI（8000）→ 项目一 frontend_vue（5173，仅登录页跳转用）→ 本项目（5174）**。
本项目不修改项目一任何代码；`.env.development` 中 `VITE_UPSTREAM_API_BASE_URL` 指向上游地址，可在设置页本地覆盖（仅存浏览器 localStorage）。

## 6. 实际调用的上游接口（全部为项目一已有接口，无新增）

| 方法 | 路径 | 功能 | 上游权限 |
| --- | --- | --- | --- |
| POST | `/auth/login` | 登录签发 token | 公开 |
| GET | `/auth/me` | 当前用户（角色） | 登录 |
| GET | `/kbs` | 知识库列表（创建数据集选 kb_id） | 登录 |
| POST | `/eval/datasets` | 创建数据集 | admin |
| POST | `/eval/datasets/{id}/items` | 批量导入样本（≤500） | admin |
| DELETE | `/eval/datasets/{id}` | 删除数据集（级联删样本，保留运行记录） | admin |
| GET | `/eval/datasets` / `/eval/datasets/{id}/items` | 数据集 / 样本分页 | login |
| POST | `/eval/runs` | 创建异步评测任务（可选 rag_config） | admin |
| DELETE | `/eval/runs/{id}` | 删除评测任务（级联删得分） | admin |
| GET | `/eval/runs`（?status=）/ `/eval/runs/{id}` | 任务列表 / 详情（含 rag_config） | login |
| GET | `/eval/runs/{id}/scores` | 得分明细分页 | login |
| GET | `/eval/runs/{id}/export` | 导出单任务 CSV | admin |

## 7. 与需求文档的实现差异（2026-10-19 更新：后端已做增量增强）

项目一 eval 模块已完成 rag_config 增量增强（见项目一交付说明），原差异表多数已消除：

| 能力 | 当前状态 |
| --- | --- |
| `rag_config`（chunk_size/search_mode/top_k） | ✅ `POST /eval/runs` 可选传入，持久化到 `eval_runs.rag_config`，列表/详情返回；**top_k / search_mode 均请求级生效**（search_mode：vector_only 纯向量 / bm25_vector_hybrid 混合），chunk_size（入库切片参数）为实验记录展示 |
| 删除数据集 / 删除任务 | ✅ `DELETE /eval/datasets/{id}`（级联删样本，保留运行记录）、`DELETE /eval/runs/{id}`（级联删得分），仅管理员 |
| 普通用户浏览 | ✅ eval GET 查询接口放开为登录可读；前端按角色显隐写按钮，后端 require_admin 二次校验 |
| CSV 上传 | ✅ 前端解析校验（表头/空行/500 上限）→ 创建数据集 + 批量导入样本 |
| BadCase 筛选 | ✅ 前端阈值过滤（后端无独立 badcase 接口） |
| 401 跳转项目一登录页 | ✅ axios 拦截器实现 |

A/B 对比功能（question 对齐、差值计算、ECharts 绘图、Markdown 导出）**100% 前端完成**；A/B 报告的实验配置现直接读取后端持久化的 `run.rag_config`。

## 8. 目录结构

```text
evalagent_vue/
├─ .env.development / .env.production   # VITE_UPSTREAM_API_BASE_URL / VITE_LOGIN_URL
├─ docs/EvalAgent-test-cases.md         # 手工测试用例
└─ src/
   ├─ api/          # http.ts（axios+拦截器）、auth/dataset/eval/kb、types.ts
   ├─ store/auth.ts # 轻量全局鉴权状态（localStorage 持久化）
   ├─ router/       # 路由 + 守卫（requiresAuth / requiresAdmin）
   ├─ components/   # AppSidebar / EmptyState / BadCaseFilter
   ├─ utils/        # csv 解析、abCompare 对齐算法、markdown 报告、download、expParams
   └─ views/        # DatasetView / TaskView / AbCompareView / SettingsView / LoginView
```

## 9. JWT 鉴权与权限

- 请求拦截器自动携带 `Authorization: Bearer ${token}`（token 存 `localStorage.evalagent_token`，与项目一前端隔离）；
- `401`：清除凭据并跳转项目一登录页（`VITE_LOGIN_URL`）；
- `403`：全局提示「权限不足：该操作仅管理员可用」；
- 写操作按钮（创建任务、删除数据集）按 `role === 'admin'` 显隐。

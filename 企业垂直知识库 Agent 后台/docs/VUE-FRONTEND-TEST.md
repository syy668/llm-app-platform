# 企业垂直知识库 Agent 后台 —— Vue 前端功能联调测试文档

> 版本：v1.1　适用范围：`frontend_vue`（Vue3 + TS + Element Plus + Vite）与 FastAPI 后端联调
> 测试基线：后端 http://127.0.0.1:8000　前端 http://localhost:5173
> 约束：本测试**不改动后端任何业务代码**，只输出测试步骤、用例与排查方法。

---

## 0. 测试范围与账号矩阵

| 类别 | 方式 | 覆盖内容 |
|---|---|---|
| 后端自动化 | `pytest -q` | 离线单元测试：trace / eval / stream(SSE) / rate_limit / retry / eval_export |
| 后端自动化 | `scripts/all_smoke_test.ps1` | 全链路 HTTP 冒烟：登录 / RBAC / 知识库 / 会话 / 四路问答 / SSE / Trace / RAGAS |
| 前端手工 | 浏览器 UI 手动 | 登录权限 / 知识库 / 智能对话 SSE / Trace / RAGAS / 系统配置 |

两套账号（普通用户由脚本或 seed 创建）：

| 账号 | 角色 | 默认来源 | 用途 |
|---|---|---|---|
| `admin` | admin | `python scripts/seed.py --username admin --password <密码>` | 全功能（含 Trace / RAGAS / 用户管理） |
| `smoke_user` | user | `scripts/all_smoke_test.ps1` 自动创建，或 `seed.py --role user` | 验证路由守卫、RBAC 越权 |

---

## 一、前置启动准备清单（分步执行命令）

### 1.1 环境依赖
- Python 3.12+、Node.js 18+、npm
- MySQL（已建 `kb_agent` 库，见 `scripts/init_db.sql`）
- Redis（生产限流需要；无 Redis 时设 `FAKE_REDIS=true`，但 **429 限流不会生效**）
- DeepSeek 或 OpenAI 兼容 LLM Key（四路问答 / RAGAS 评测需要；测试可用 `LLM_PROVIDER=mock`）

### 1.2 分步命令

```powershell
# ① 初始化数据库（首次或重建）
mysql -uroot -p < scripts/init_db.sql

# ② 准备环境变量
# 复制 .env.example 为 .env，按实际填写 DATABASE_URL / REDIS_URL / SECRET_KEY / LLM_*
copy .env.example .env

# ③ 后端依赖 + 虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt

# ④ 初始化账号（管理员 + 可选普通用户）
python scripts/seed.py --username admin --password "你的管理员密码" --display-name 系统管理员
python scripts/seed.py --username smoke_user --password "SmokeTest@2026" --role user --display-name 冒烟普通用户

# ⑤ 启动后端（另开一个终端）
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# ⑥ 前端依赖 + 类型检查 + 启动
cd frontend_vue
npm install
npm run typecheck      # vue-tsc --noEmit，先过类型门禁
npm run dev            # http://localhost:5173
```

> 说明：前端不设 Vite 代理，直接连后端（`frontend_vue/.env.development` 中 `VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1`），跨域由后端 `CORS_ORIGINS=*` 放行。

---

## 二、后端冒烟 & 单元测试执行步骤（PowerShell）

```powershell
# ① 健康检查（确认后端已起）
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
# 期望：{"code":0,"message":"ok","data":{"status":"ok","db":"ok","redis":"ok"}}

# ② 单元测试（离线，不依赖 MySQL/Redis/DeepSeek，conftest 已注入 FAKE_REDIS=true）
pytest -q

# ③ 全链路冒烟（需要真实 MySQL/Redis/LLM；普通用户 smoke_user 自动创建）
.\scripts\all_smoke_test.ps1 -AdminPass "你的管理员密码"

# ④ 冒烟但跳过耗时的 RAGAS 评测段
.\scripts\all_smoke_test.ps1 -AdminPass "你的管理员密码" -SkipRagas

# ⑤ 分模块专项冒烟（可选，单独重跑某模块）
.\scripts\smoke_test_stream.ps1 -AdminPass "你的管理员密码"
.\scripts\smoke_test_trace.ps1  -AdminPass "你的管理员密码"
.\scripts\smoke_test_eval.ps1   -AdminPass "你的管理员密码"
```

判定标准：`pytest -q` 全绿；`all_smoke_test.ps1` 结尾输出 `失败: 0`（`[WARN]` 项为 LLM 路由分类波动，需人工确认，不阻断）。

---

## 三、Vue 前端手工功能测试用例

> 表格列：测试模块 / 用例编号 / 测试步骤 / 预期现象 / 异常判断点 / 是否通过。
> 「是否通过」预留勾选：`☐`。打开浏览器 DevTools Network 面板便于核对请求与状态码。

### 模块 1：登录与权限（管理员 / 普通用户）

| 测试模块 | 用例编号 | 测试步骤 | 预期现象 | 异常判断点 | 是否通过 |
|---|---|---|---|---|---|
| 登录权限 | LOGIN-001 | 打开 http://localhost:5173（未登录）→ 跳转 `/login` | 显示登录页，含用户名/密码/角色下拉 | 直接进入内容页说明 `requiresAuth` 守卫失效 | ☐ |
| 登录权限 | LOGIN-002 | 管理员账号密码 → 点击「登 录」 | 提示「登录成功」，跳转 `/knowledge`，侧边栏出现全部菜单（含 Trace / RAGAS / 系统配置） | 侧边栏缺管理员菜单，检查 `useUserStore.isAdmin` | ☐ |
| 登录权限 | LOGIN-003 | 普通用户账号密码登录 | 跳转 `/knowledge`，顶栏显示「普通用户」，**侧边栏无 Trace / RAGAS** | 出现管理员菜单说明 `App.vue` 的 `store.isAdmin` 判断异常 | ☐ |
| 登录权限 | LOGIN-004 | 输入错误密码登录 | 弹「登录失败」类错误，停留登录页，不写 localStorage | 错误仍跳转说明后端返回被误判 | ☐ |
| 登录权限 | LOGIN-005 | 用户名/密码留空直接登录 | 弹「请输入用户名和密码」警告，**不发请求** | Network 出现 `/auth/login` 请求说明前端校验失效 | ☐ |
| 登录权限 | LOGIN-006 | 登录页「角色」下拉切换管理员/普通用户后登录 | 角色下拉**仅演示**（下方灰字已说明），实际角色由后端账号决定 | 若切换角色改变了后端权限则为异常 | ☐ |
| 登录权限 | LOGIN-007 | 未登录直接访问 `/chat`、`/trace`、`/eval` | 全部重定向 `/login?redirect=原路径` | 能直接看到页面内容为守卫失效 | ☐ |
| 登录权限 | LOGIN-008 | 已登录访问 `/login` | 自动跳转 `/knowledge` | 停留在登录页为异常 | ☐ |
| 登录权限 | LOGIN-009 | 登录后手动清除 localStorage 的 `kb_agent_access_token` → 刷新 | 触发 401 → 自动用 refresh_token 换新 → 无感续期 | 反复弹「登录已过期」说明刷新链路失败 | ☐ |
| 登录权限 | LOGIN-010 | 普通用户手动输入 URL `/trace`、`/eval` | 弹「权限不足：仅管理员可访问该页面」，重定向 `/knowledge` | 能进入页面为**前端守卫失效**（后端仍应 403 兜底） | ☐ |
| 登录权限 | LOGIN-011 | 点击侧边栏底部退出按钮 | 弹「已退出登录」，清空本地态，回到 `/login` | localStorage 残留 token 为异常 | ☐ |

### 模块 2：知识库管理（管理员 / 普通用户）

| 测试模块 | 用例编号 | 测试步骤 | 预期现象 | 异常判断点 | 是否通过 |
|---|---|---|---|---|---|
| 知识库 | KB-001 | 管理员登录 → `/knowledge` | 左侧知识库列表加载；有数据则默认选中第一条并加载右侧统计 | 列表空白且控制台报错，检查 `/kbs` 与 `/kbs/{id}/stats` | ☐ |
| 知识库 | KB-002 | 点击「新建知识库」→ 填名称/描述 → 创建 | 弹「创建成功」，列表刷新出现新库 | 名称空时仍创建为校验失效（应弹「请填写知识库名称」） | ☐ |
| 知识库 | KB-003 | 新建弹窗名称留空直接创建 | 弹「请填写知识库名称」，不发请求 | Network 出现 `POST /kbs` 为异常 | ☐ |
| 知识库 | KB-004 | 选中知识库 → 「文档管理」→ 选择 .txt/.md/.pdf 上传并解析 | 弹「上传成功」，文档列表出现新文档，解析状态「已完成」，统计数字更新 | 上传报 422/500，检查 multipart 字段名 `file` | ☐ |
| 知识库 | KB-005 | 选择不支持格式（如 .exe）上传 | 文件选择器按 `accept` 过滤，无法选入 | 能选中并上传非白名单格式为异常 | ☐ |
| 知识库 | KB-006 | 文档列表翻页 / 改每页条数 | 分页正常，`page`/`size` 正确传递 | 总页数异常，核对 `Paginated.total` | ☐ |
| 知识库 | KB-007 | 点击文档「分块」 | 弹窗展示分块列表，每块含 `chunk_index` / token 数 / 内容 | 弹窗空或报错，检查 `/documents/{id}/chunks` | ☐ |
| 知识库 | KB-008 | 「参数配置」标签 → 改分块大小/top_k → 保存配置 | 弹「已更新」，列表元数据刷新 | 未保存即刷新为异常，核对 `PUT /kbs/{id}` | ☐ |
| 知识库 | KB-009 | 点击文档「删除」 | **先弹确认框**（含文档名，说明分块与向量一并清除）→ 确认后弹「文档已删除」，文档消失，统计更新；点「取消」不发请求 | 无确认框直接删除为异常（v1.4 已加） | ☐ |
| 知识库 | KB-010 | 「删除知识库」标签 → 点击删除 | **先弹红色确认框**（含库名，写明级联清除文档/分块/向量且不可恢复）→ 确认后删除并回到空态；点「取消」不发请求 | 无确认框直接删除为异常（v1.4 已加，与会话/用户删除确认风格一致） | ☐ |
| 知识库 | KB-011 | 普通用户登录，访问管理员创建的知识库 | 列表默认只显示自己知识库；越权访问他人知识库接口返回 403 | 普通用户能看到/操作他人知识库为 RBAC 失效 | ☐ |

### 模块 3：智能对话 SSE（管理员 / 普通用户）

| 测试模块 | 用例编号 | 测试步骤 | 预期现象 | 异常判断点 | 是否通过 |
|---|---|---|---|---|---|
| 智能对话SSE | CHAT-001 | `/chat` 页面加载 | 左栏历史会话列表加载，右栏参数面板显示 | 会话列表空且报错，检查 `/conversations` | ☐ |
| 智能对话SSE | CHAT-002 | 点击「新建」→ 选择/不选知识库 → 创建 | 新会话创建成功并自动选中，消息区清空 | 未选中新会话为异常 | ☐ |
| 智能对话SSE | CHAT-003 | **未选择任何会话**，观察输入框与发送按钮 | 中栏显示「请左侧选择会话」空态，输入框与发送按钮均 **disabled**，Network **无任何 `/chat/stream` 请求** | 出现 422 或 `/chat/stream` 请求 = 空会话守卫失效（风险点1） | ☐ |
| 智能对话SSE | CHAT-004 | 选中会话但输入框为空 → 点发送 | 发送按钮 disabled，不发请求 | 发出空 `question` 请求（后端会 422）为异常 | ☐ |
| 智能对话SSE | CHAT-005 | 选中会话，输入**中文问题**（如「查询所有工单」）发送 | AI 气泡逐字流式输出中文，无 `�` / 乱码 / 半个汉字 | 乱码需检查 `TextDecoder('utf-8', {stream:true})` 分帧（风险点3） | ☐ |
| 智能对话SSE | CHAT-006 | 观察流式期间的 `RouteTag` 标签 | 根据意图显示 `rag` / `db_query` / `report` / `direct` 之一 | 标签缺失或不匹配，核对 `event: route` | ☐ |
| 智能对话SSE | CHAT-007 | 提问「生成本月工单汇总报表」 | `report` 路由：气泡渲染 Markdown 表格，含「报表由系统自动生成」尾注，非打字机 | 未渲染 Markdown（表格乱成文本）检查 `renderMarkdown` | ☐ |
| 智能对话SSE | CHAT-008 | 一次问答完成后刷新页面 / 重进会话 | 历史消息（含 `agent_route`、`trace_id`）正确回显，AI 内容为渲染后的 Markdown | 历史为空说明 `done` 事件未落库，核对 `/conversations/{id}/messages` | ☐ |
| 智能对话SSE | CHAT-009 | 触发失败场景（如后端停 LLM / 非法会话） | 失败以红色文本显示，或弹错误提示，不卡「生成中…」 | 一直转圈无反馈为异常 | ☐ |
| 智能对话SSE | CHAT-010 | 打开 DevTools Network，逐个核对请求 URL | 所有请求形如 `/api/v1/conversations/1/chat/stream`，**无 `//`** | URL 含 `/api/v1//xxx` 并 404 = `joinUrl` 失效（风险点2） | ☐ |
| 智能对话SSE | CHAT-011 | 真实 Redis 下把 `AI_RATE_LIMIT_PER_MINUTE` 调小（如 3），连续发 4 次 | 第 4 次弹「请求过于频繁，请稍后重试」，SSE 场景下 AI 气泡显示失败 | `FAKE_REDIS=true` 时不限流属预期；真实 Redis 仍放行=限流失效（风险点4） | ☐ |
| 智能对话SSE | CHAT-012 | 选中会话 → 点击「删除会话」 | **先弹确认框**（红色删除按钮，提示消息将一并清除且不可恢复）；点「取消」无 DELETE 请求；点「删除」后弹「已删除」，会话与消息消失 | 不弹确认框直接删除为异常 | ☐ |
| 智能对话SSE | CHAT-013 | 右栏「参数配置」：调整 top_k、切换「检索知识库」、关闭「业务库查询」后发送提问 | 控件可编辑；请求体携带 `top_k` / `kb_id`（选择知识库时）/ `route_enabled`；**顶部知识库标签实时跟随**：临时指定时显示橙色「KB#N · 临时」，清空后恢复蓝色会话绑定库；命中被关闭路由的问题直接由 AI 回答（无工具调用标签）；未选择知识库时请求体无 `kb_id`（跟随会话绑定） | 控件 disabled、切库后顶部标签不变为异常（v1.4 已修复） | ☐ |
| 智能对话SSE | CHAT-014 | 左栏底部「重命名」→ 输入新标题保存 | 弹「已重命名」，列表标题立即更新，**中栏顶部标题同步变为新标题**，刷新后保持；空标题 / 超 100 字被拦截 | 顶部仍显示旧标题或「会话 #id」为异常（v1.3 已修复） | ☐ |
| 智能对话SSE | CHAT-015 | AI 回答气泡右上角点击「复制原文」按钮 | 提示「已复制到剪贴板」，粘贴得到 Markdown 源文本；失败消息（红色）无复制按钮 | 无按钮或粘贴为空为异常 | ☐ |

### 模块 4：Trace 链路日志（仅管理员）

| 测试模块 | 用例编号 | 测试步骤 | 预期现象 | 异常判断点 | 是否通过 |
|---|---|---|---|---|---|
| Trace链路 | TRACE-001 | 管理员进入 `/trace` | Trace 列表分页加载，展示 trace_id / 路由 / 状态 / tokens / 耗时 | 列表空且报错，检查 `/traces` | ☐ |
| Trace链路 | TRACE-002 | 按路由 / 状态 / 用户ID 筛选 → 查询 / 重置 | 筛选生效，参数 `route`/`status`/`user_id` 正确传递 | 筛选无效核对 query 参数 | ☐ |
| Trace链路 | TRACE-003 | 点击行展开箭头 | 展开详情：模型 / 耗时 / tokens / 工具调用数 / 异常信息 | 展开无数据，检查 `/traces/{trace_id}` | ☐ |
| Trace链路 | TRACE-004 | 展开某条 `db_query` 链路，查看「工具调用明细」 | 展示 `db_query` 工具步骤、输入/输出、状态、耗时 | 工具调用为空，核对 `tool_call_count` 与 `/tool-calls` | ☐ |
| Trace链路 | TRACE-005 | 触发一次会失败的 db_query 重试（注入异常），再查该 trace 工具明细 | **重试的失败尝试以 `status=failed` 完整写入**（次数 = 重试次数），最终成功/失败各一条 | 只有成功记录、无 failed 中间记录 = 重试未落库（风险点5） | ☐ |
| Trace链路 | TRACE-006 | 切换「统计汇总」标签 | 显示调用量 / 平均耗时 / Token 总量 / LLM 调用次数 / 路由分布条形图 | 汇总全 0 或报错，核对 `/traces/stats/summary` | ☐ |
| Trace链路 | TRACE-007 | 「工具调用明细」标签：点击列表某行 Trace ID（复制完整 ID）粘贴查询，或直接点行内「明细」按钮 | 展示该 trace 的工具调用表格；direct 直答路由的 trace 明细为空属正常（未经过工具节点）；输入空 ID 应弹「请输入 Trace ID」警告 | 输入前 12 位截断 ID 查不到数据 = 未复制完整 ID；「明细」按钮未自动查询为异常（v1.4 已加） | ☐ |
| Trace链路 | TRACE-008 | 普通用户直接访问 `/trace`（或 curl 带普通 token 请求 `/api/v1/traces`） | 前端重定向 `/knowledge` 并提示权限不足；后端返回 **403** | 后端未 403 = `require_admin` 二次鉴权失效（风险点6） | ☐ |
| Trace链路 | TRACE-009 | 无任何 Trace 数据时打开页面 | 表格显示空态「暂无 Trace 记录」，不报错 | 空态异常或控制台报错 | ☐ |

### 模块 4b：用户管理（仅管理员，v1.4 新增）

| 模块 | 编号 | 操作步骤 | 预期结果 | 排查提示 | 完成 |
|---|---|---|---|---|---|
| 用户管理 | USR-001 | 管理员进入 `/users`（侧边栏新增「用户管理」） | 用户列表分页加载：ID / 用户名 / 显示名 / 邮箱 / 角色 / 状态 / 创建时间 | 普通用户访问应被重定向且提示权限不足（`requiresAdmin`） | ☐ |
| 用户管理 | USR-002 | 新建用户：用户名 ≥3 字符、密码 ≥8 位、选角色 | 弹「用户已创建」，列表出现新账号 | 用户名重复 / 密码过短由后端 400 拦截 | ☐ |
| 用户管理 | USR-003 | 编辑用户改显示名 / 邮箱 / 角色 / 状态 → 保存 | 弹「已保存」，列表即时更新；自己这行「删除」按钮置灰 | 把自己降级 / 禁用后仍持有 admin 界面 = 前端未防呆 | ☐ |
| 用户管理 | USR-004 | 点「重置密码」输入新密码 ≥8 位 | 弹「密码已重置」，用新密码可登录 | — | ☐ |
| 用户管理 | USR-005 | 点「删除」→ 确认框说明软删除语义 → 确认 | 弹「已禁用该账号」，该用户状态变「禁用」；禁用账号无法登录 | 删除自己应 409（后端守卫） | ☐ |
| 修改密码 | USR-006 | 顶栏用户信息区点钥匙图标 → 填旧密码 + 新密码（两次确认）→ 保存 | 弹「密码已修改」，新密码下次登录生效；新密码 <8 位或两次不一致被前端拦截 | 旧密码错误由后端 400 提示 | ☐ |

### 模块 5：RAGAS 评测（仅管理员）

| 测试模块 | 用例编号 | 测试步骤 | 预期现象 | 异常判断点 | 是否通过 |
|---|---|---|---|---|---|
| RAGAS评测 | EVAL-001 | 管理员进入 `/eval` | 顶部 4 张统计卡片（任务总数 / 运行中 / 平均 Faithfulness / 平均 Context Recall）+ 任务表格加载 | 卡片全 `-` 或报错，核对 `/eval/runs` | ☐ |
| RAGAS评测 | EVAL-002 | 已有数据集时，点击「新建评测任务」选择数据集 → 开始评测 | 弹「已触发评测 run_id=.. 状态=pending（异步执行）」，列表刷新 | 无数据集时应提示「请选择数据集」 | ☐ |
| RAGAS评测 | EVAL-003 | 观察评测任务状态流转 pending → running → done | 状态标签颜色/文案随之变化，「成功/总样本」更新 | 卡在 running 超时，检查后台任务与 LLM | ☐ |
| RAGAS评测 | EVAL-004 | 展开一条 done 任务行 | 展示得分明细表：faithfulness / answer_relevancy / context_precision / context_recall | 展开空，核对 `/eval/runs/{id}/scores` | ☐ |
| RAGAS评测 | EVAL-005 | done 任务行点击「CSV」按钮 | 浏览器下载 `ragas_run_{id}.csv`，**Excel/WPS 打开中文不乱码**，表头完整 | 下载失败/空文件/中文乱码 = CSV 下载异常（风险点7） | ☐ |
| RAGAS评测 | EVAL-006 | 点击「批量执行」 | 对所有含样本的数据集逐个触发评测，提示成功个数 | 单条失败不应中断整批 | ☐ |
| RAGAS评测 | EVAL-007 | 普通用户直接访问 `/eval`（或 curl 带普通 token 请求 `/api/v1/eval/datasets`） | 前端重定向并提示权限不足；后端返回 **403** | 后端未 403 = `require_admin` 失效（风险点6） | ☐ |
| RAGAS评测 | EVAL-008 | 用 curl 验证后端 CSV 导出接口 | `GET /api/v1/eval/runs/{id}/export` 返回 `text/csv`，含 `Content-Disposition: attachment`，文件头带 UTF-8 BOM | 401/403（无 admin token）、404（run 不存在）需区分 | ☐ |

> **已统一（v1.1）**：前端 `src/api/eval.ts` 的 `exportRunScoresCsv` 已改为直接调用后端 `GET /eval/runs/{run_id}/export`（`app/api/v1/eval_export.py`），以 `responseType: 'blob'` 原样接收带 UTF-8 BOM 的 CSV 并触发浏览器下载。相比原先本地从 `/scores` 拼接，后端导出列更全（含 `retrieved_contexts`），BOM 处理统一。

### 模块 6：系统配置页（管理员 / 普通用户）

| 测试模块 | 用例编号 | 测试步骤 | 预期现象 | 异常判断点 | 是否通过 |
|---|---|---|---|---|---|
| 系统配置 | CFG-001 | 管理员进入 `/config` | 页面加载真实配置（读取 `GET /system/config`），显示 AI 限流 / 工具重试与超时 / LLM 参数卡片，顶部提示「保存后立即生效，无需重启服务」 | 卡片数值全为 0 或报「网络异常」为异常 | ☐ |
| 系统配置 | CFG-002 | 管理员修改限流阈值（如 30→20）→ 点击「保存配置」 | 弹「配置已保存并实时生效」，刷新页面数值保持 20；Network 可见 `PUT /system/config` 返回 code=0 | 保存后刷新回旧值 = 持久化失败（检查 `data/system_config.json`） | ☐ |
| 系统配置 | CFG-003 | 管理员在 LLM 卡片：模型输入自定义名（如 `qwen-max`）、填写 API Base 与 API Key → 保存 | 均可保存；保存后 API Key 输入框清空且占位提示「已配置，留空保持不变」，**不回显明文**；再次保存（不重输 Key）后密钥仍生效 | API Key 明文回显或重开页面后丢失 = 异常 | ☐ |
| 系统配置 | CFG-004 | 普通用户进入 `/config` | 表单只读（disabled），顶部提示「仅管理员可修改」，无「保存配置」按钮 | 普通用户可编辑或出现保存按钮为越权异常 | ☐ |
| 系统配置 | CFG-005 | 普通用户用 DevTools 直接调 `PUT /system/config` | 返回 403「仅管理员可执行该操作」 | 返回 200 为后端越权漏洞 | ☐ |

---

## 四、常见问题排查清单

| 现象 | 可能原因 | 排查动作 |
|---|---|---|
| 跨域 CORS 报错 | 后端 `CORS_ORIGINS` 未放开 | 确认 `.env` 中 `CORS_ORIGINS=*`（生产改具体域名）；或启用 `vite.config.ts` 中注释的 proxy |
| 前端直连失败（连接被拒） | 后端未启动 / 端口不对 | `Invoke-RestMethod http://127.0.0.1:8000/api/v1/health`；核对 `.env.development` 的 `VITE_API_BASE_URL` |
| SSE 流式断开 / 只出首包 | 代理/缓冲导致 | 后端已设 `X-Accel-Buffering: no`；前端用 fetch+ReadableStream，非 EventSource；确认无 Vite 代理截断 |
| SSE 中文乱码 | 解码未按 UTF-8 分帧 | 前端 `sse.ts` 已用 `TextDecoder('utf-8', {stream:true})` + `\n\n` 分帧；若乱码检查后端 `media_type="text/event-stream"` 与响应编码 |
| 401 未登录 / token 过期 | token 缺失、伪造、过期 | 前端拦截器自动 refresh 重试一次；检查 `useUserStore` localStorage 的 access/refresh token；`SESSION_TTL_SECONDS` 到期 |
| 401 循环刷新 | refresh_token 也失效 | 清空 localStorage 重新登录；检查 `SECRET_KEY` 是否变更（变更后旧 token 全失效） |
| 403 权限不足 | 普通用户访问管理接口 / 越权访问他人资源 | 前端 `requiresAdmin` 守卫 + 后端 `require_admin` 双层；核对账号真实 `role`（登录页角色下拉不影响后端） |
| 429 请求过于频繁 | Redis 令牌桶限流命中 | 确认 `FAKE_REDIS=false` 且真实 Redis 连通；`AI_RATE_LIMIT_PER_MINUTE` 控制阈值；Redis 异常时放行（不报错） |
| 文件上传失败 / 422 | multipart 字段名或格式不符 | 字段名必须为 `file`；`accept=".pdf,.docx,.xlsx,.txt,.md"`；检查 `python-multipart` 已安装 |
| CSV 下载乱码 | 缺 UTF-8 BOM | 后端导出接口已前置 BOM；前端本地生成也加了 `﻿`；用 Excel 而非纯文本打开验证 |
| 页面组件找不到 / 白屏 | 未安装依赖 / 路径别名失效 / TS 报错 | `npm install`、`npm run typecheck`；确认 `vite.config.ts` 的 `@` 别名指向 `./src` |
| 前端菜单缺少管理员项 | 角色未识别 | 检查登录返回的 `user.role`，`isAdmin = user.role === 'admin'` |

---

## 五、历史遗留风险点专项核对表（8 项）

| # | 风险点 | 代码锚点 | 验证结论 |
|---|---|---|---|
| 1 | 会话为空不发起请求，无 422 | `Chat.vue` `send()` 守卫 + 输入框 `disabled` + 空态分支 | 已由前端三重守卫兜住（CHAT-003/004） |
| 2 | URL 拼接无 `//` 404 | `request.ts` `joinUrl` + `API_BASE` 去尾斜杠 | `sse.ts`/`store` 均走 `joinUrl`（CHAT-010） |
| 3 | SSE 中文不乱码 | `sse.ts` `TextDecoder('utf-8',{stream:true})` 分帧 | 逐字输出，多字节安全（CHAT-005） |
| 4 | Redis 令牌桶 429 | `rate_limit.py` `rate_limit_ai` 挂载 chat/stream | 需 `FAKE_REDIS=false`（CHAT-011） |
| 5 | 工具重试完整写入 Trace | `retry.py` `with_retry` + `_record_failed_attempt` | failed 尝试逐条落 `trace_tool_calls`（TRACE-005） |
| 6 | 普通用户拦截 Trace/RAGAS + 后端二次鉴权 | `router/index.ts` `requiresAdmin` + `deps.py` `require_admin` | 前端重定向 + 后端 403 双层（LOGIN-010 / TRACE-008 / EVAL-007） |
| 7 | RAGAS CSV 下载正常 | 后端 `eval_export.py` `/runs/{id}/export`；前端 `eval.ts` 走该接口 blob 下载 | 前端已统一走后端导出接口，列全、带 BOM（EVAL-005/008） |
| 8 | ~~系统配置页全禁用、仅展示~~（v1.3 已解除） | v1.2 起后端提供 `GET/PUT /system/config`（运行时配置热更新），v1.3 支持自定义模型 / API 地址 / API Key；普通用户仍只读 | 管理员可编辑并保存、普通用户只读 + 后端 403 双层（CFG-001~005） |

---

## 六、项目收尾确认检查项（git 提交 / README 准备）

**提交前：**
- [ ] `pytest -q` 全绿；`all_smoke_test.ps1` 通过（`失败: 0`）
- [ ] 前端 `npm run typecheck` 无 TS 报错；`npm run build` 可出产物
- [ ] 8 项风险点逐条勾选通过
- [ ] `.env` / `.env.example` 已脱敏，**不提交真实密钥**（确认 `.gitignore` 覆盖 `.env`、`uploads/`、`chroma_db/`、`node_modules/`）
- [ ] 清理测试产生的临时数据（冒烟脚本已自动删除会话/知识库，若有残留手工清理）

**README 需补：**
- [ ] 快速启动四步：建库 → seed 管理员 → 起后端 → 起前端
- [ ] 两套账号说明（admin / smoke_user）与初始化命令
- [ ] 后端测试命令（`pytest -q`、`all_smoke_test.ps1 -AdminPass ...`）与前端测试入口（`npm run dev` + 本用例文档）
- [ ] 8 项历史风险点与对应验证结论
- [ ] 差异备注：CSV 下载已统一走后端 `/eval/runs/{id}/export`（blob 接收，带 BOM，含 `retrieved_contexts` 列）

**git 提交建议：**
- [ ] 拆分为语义化提交：前端页面与组件 / 后端新增模块 / 测试与文档，避免混入 `.env` 与产物
- [ ] 提交信息末尾加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`

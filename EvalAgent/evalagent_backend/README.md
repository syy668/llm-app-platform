# evalagent_backend

EvalAgent 独立后端：评测集管理 + RAGAS 异步评测。**算力隔离**于项目一
——项目一只负责「答题」，项目二自己跑裁判模型算分、自己落库。

## 当前进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 一 | Monorepo 目录重构 | ✅ |
| 二 | `eval_agent_db` 建库 + 四表建模 | ✅ |
| 三 | RAGAS 评测引擎 + 4 个聚合读接口 | ✅ |
| 四 | 前端/项目三切基址到 8200（E2E 21/21） | ✅ |
| 五 | 裁判配置管理：`runtime_configs` 落库 + 热更新 + 保存即探活 | ✅ |

## 评测链路（第三步）

```
POST /eval/runs
   └─ runner.execute_run（后台任务）
        1. judge.preflight()            裁判探活，配置错立刻失败
        2. rag_client.query()  × N      httpx 并发调项目一 /kbs/{id}/query
        3. judge.score()       × N      RAGAS 四项指标（to_thread 并发）
        4. 逐条写 eval_scores            processed_items 实时递增，前端进度条可动
        5. aggregate → eval_runs.metrics 全部为 NULL 时置 failed，不写全 null dict
```

被测系统（项目一）与裁判（本仓库）**物理隔离**：换被测系统不动打分口径，换裁判不动取数逻辑。

## 目录

```
app/
├── main.py               # FastAPI 实例、lifespan 建表、统一异常信封、健康检查
├── core/
│   ├── config.py         # pydantic-settings 配置（.env 优先）
│   ├── database.py       # engine / SessionLocal / Base / 探活
│   └── response.py       # {code, message, data} 信封 + 分页体
├── models/               # 四张表的 ORM
├── schemas/              # 出入参 Pydantic 模型
├── services/serialize.py # ORM → 响应字典（含旧字段别名，兼容前端 v1 视图）
└── api/v1/               # datasets / runs 路由
scripts/
├── init_db.py            # 建库建表（幂等）
└── dump_schema.py        # 导表结构 JSON（含列注释）
```

## 数据模型（与项目一的差异）

| 差异点 | 项目一 kb_agent | 项目二 eval_agent_db |
| --- | --- | --- |
| 评测对象 | `eval_runs.kb_id` 必填 | `target` + `target_ref` 三元组，支持 kb / agentflow / multi_agent |
| 多智能体 | 无 | `agent_scope`（角色+权重）、`eval_scores.agent_role` / `agent_trace`、`eval_items.expected_agents` |
| 成本与链路 | 无 | `total_tokens` / `cost_usd` / `trace_id` |
| 跨系统寻址 | 无 | `eval_runs.ext_run_ref` 承接项目三 `callback_run_no` |
| 可疑标记 | 无 | `eval_scores.suspect`（引用/数字校验，只标不拦） |
| 分层评测 | `stratum` | 保留，并在 `eval_scores` 里快照一份 |

主键统一 `bigint auto_increment`，字符集 `utf8mb4 / utf8mb4_unicode_ci`。

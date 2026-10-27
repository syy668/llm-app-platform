# AgentFlow Platform — W4 交付说明（评测体系 + 种子校验 + KB 适配层 + 压缩 A/B + Demo）

> 本阶段**只新增文件，未修改任何 W1/W2/W3 业务代码**。
> 唯一触碰存量代码的位置是 `app/tools/mcp_servers/kb_search.py` 里 `KbSearchTool.__init__` 的 provider 选择（7 行，含注释，延迟 import 防循环），
> 属任务书明确要求的「kb-search 适配层改造」，MCP 外层框架、工具签名、`MOCK_EXTERNAL_RAG=true` 的行为均未变动。

---

## 1. 本阶段完成什么

| # | 任务 | 交付物 | 状态 | 验收证据 |
|---|---|---|---|---|
| 1 | 分层难例评测集与跑批 | `eval_datasets/分层难例评测集-manifest.json`（30 条）+ `scripts/eval_runner.py` | 完成 | 30/30 跑通，成功率 100%，产物 4 份落 `eval_output/` |
| 2 | 种子数据硬校验 | `scripts/verify_seed.py` | 完成 | **26/26 通过**，`SEED VERIFIED`，exit=0，29.5s |
| 3 | kb-search 适配层改造 | `app/tools/kb_adapter.py`（新增）+ `kb_search.py`（1 行 provider 选择） | 完成 | mock 走原逻辑；non-mock 命中 SKU-10086 断货；熔断打开后兜底不空 |
| 4 | 上下文压缩 A/B | `scripts/ab_compression.py` | 完成 | 8 样例 ×2 组；微基准压缩率 **73.2%**；端到端 token **+37.7%**（原因见 §6.4） |
| 5 | 交付文档 | `README-W4.md`、`W4-自测核对清单.md`、`AB实验报告模板.md`、`W1-W4项目总览与总结.md` | 完成 | 本文件 |
| 6 | Demo 三条链路 | `scripts/demo_three_flows.py` | 完成 | **3/3 链路、21/21 步骤通过** |

公共基建：`scripts/w4_common.py`（1021 行）—— 嵌入式 harness、任务 API 客户端、候选指标函数、judge、输出工具。
四个脚本全部复用同一份 harness，保证「评测 / 校验 / A/B / Demo」跑在**同一套确定性环境**里。

**回归**：`pytest` **150 passed / 0 failed**（9.39s），W1 鉴权 12 条、W2 内核、W3 SSE/审批/前端用例全部原样通过。

---

## 2. W4 新增文件清单

| 文件 | 行数 | 职责 |
|---|---|---|
| `eval_datasets/分层难例评测集-manifest.json` | 512 | 30 条分层难例（L1~L6），含 `question / ground_truth / risk_level / case_type / expected_gmv / expected_keywords / needs_notify` |
| `scripts/w4_common.py` | 1021 | 嵌入式 harness（SQLite+WAL / fakeredis / InMemoryTransport / InMemorySaver / ASGITransport）、`TaskApi`、指标函数、`JudgeClient`、输出工具 |
| `scripts/eval_runner.py` | 571 | 评测跑批：提交→轮询终态→采集指标→聚合→出 csv/json/md |
| `scripts/verify_seed.py` | 711 | 种子数据 3 条硬断言 + 20 个随机断点抽样恢复校验 |
| `scripts/ab_compression.py` | 611 | 压缩 A/B：微基准 + 端到端图级对照 + 指标汇总表 + markdown 报告 |
| `scripts/demo_three_flows.py` | 823 | 三条演示链路（审批闭环 / 驳回重规划 / 取消冻结） |
| `app/tools/kb_adapter.py` | 475 | `CircuitBreaker` / `MallKbProvider` / `ResilientKbProvider` / `build_kb_provider` / `default_mall_dsn` |

产物目录（全部为运行期生成，已在 `.gitignore` 语义上属于可重建文件）：

```
eval_output/
├── eval_detail_full.json        # 30 条逐样例明细
├── eval_summary_full.csv        # 逐样例汇总表
├── eval_metrics_full.csv        # 全局指标
├── eval_report_full.md          # 人读报告
├── ab_samples_full.csv          # A/B 逐样例
├── ab_metrics_full.csv          # A/B 16 项指标对照
├── ab_detail_full.json
├── AB实验报告_full.md
├── demo_detail_offline.json
└── Demo三条链路_offline.md
```

---

## 3. 评测体系（任务 1）

### 3.1 评测集怎么分层

| 层 | 条数 | 考察点 | 典型埋因 |
|---|---|---|---|
| L1 单因归因 | 8 | 能否锁定唯一主因 | 数码配件转化率 3.1%→1.7%、GMV 环比 -18% |
| L2 双因归因 | 6 | 能否同时给出两条带数证据 | 预算 12000→7200 + SKU-10086 断货 |
| L3 高危需审批 | 6 | 涉及「推送/通知」时是否被审批拦住 | `risk_level=high` + `needs_notify=true` |
| L4 知识库依赖 | 4 | 是否检索历史周报佐证 | kb-search 命中片段 |
| L5 噪声异常 | 3 | 干扰项不被当主因 | 无关品类波动 |
| L6 边界 | 3 | 空窗期 / 单品类 / 极短时间窗 | 时间窗边界 |

`risk_level` 分布：normal 24 / high 6。评测集数字与 `verify_seed.py` 的种子夹具**同源**，
所以「评测里说的 -18%」和「库里存的 -18%」是同一份事实，避免自证式评测。

### 3.2 指标口径（写在报告头部，避免口径漂移）

| 指标 | 定义 | 实现位置 |
|---|---|---|
| 任务成功率 | 终态 ∈ {COMPLETED} 的比例 | `aggregate()` |
| 超时率 | 超过 `--timeout` 未到终态的比例（**超时重试 2 次仍失败才计 timeout**，且不打断批量） | `run_sample()` |
| GMV 结论准确率 | **四项全过**才算对：趋势一致 + 幅度落在 `pct_range` + 必提词齐全 + 无禁用论断 | `score_gmv_conclusion()` |
| GMV 分项 | 趋势 / 幅度 / 必提词 / 无禁用 四项单独统计，便于定位「全对但只差一个词」 | 同上 |
| 重规划次数 | `replan_count` 均值 / 总和 / 涉及样例数 | `aggregate()` |
| 分节点 token | `detail.token_ledger[node].input + output` | `node_tokens()` |
| P50 / P95 时延 | nearest-rank 分位 | `percentile()` |
| faithfulness | LLM-as-judge（**生成模型与评审模型分离**）；无 key 时降级本地 2-gram 支撑率 | `JudgeClient` |
| 上下文召回率 | `expected_keywords` 在报告中的命中率 | `keyword_recall()` |

### 3.3 实测结果（`--tag full`，30 条，54s）

| 指标 | 值 |
|---|---|
| 任务成功率 | **100%（30/30）** |
| 超时率 | 0%（0 条） |
| GMV 结论准确率（四项全过） | 0.0% |
| └ 趋势 / 幅度 / 必提词 / 无禁用 | 93.3% / 0.0% / 13.3% / 100.0% |
| 重规划次数 | 0.0（均值）/ 0 / 0 条 |
| 总 token / 单样例均值 | 98942 / 3298.07 |
| LLM 调用均值 | 4.17 |
| P50 / P95 | 433 ms / 721 ms |
| faithfulness | 0.31（`local_rule(no_key)`） |
| 上下文召回率 | 0.35 |

> **为什么 GMV 准确率是 0，而趋势却有 93%？**
> 离线模式 `LLM_MOCK=true`，`summarizer` 输出的是**固定模板**（不含具体百分比、不含品类名）。
> 趋势能从「下滑/gmv」这类词勉强对齐，但「环比 -18%」「数码配件」这类硬约束必然落空。
> 这恰恰是「离线只能验证管线、不能验证质量」的铁证 —— 报告头部已明确标注，
> 并给出真实模型复跑命令（`--llm real` + `JUDGE_API_KEY`）。

### 3.4 用法

```bash
# 离线冒烟（默认 embedded）
python scripts/eval_runner.py --tag smoke --limit 5

# 全量 30 条（约 1 分钟）
python scripts/eval_runner.py --tag full

# 打真实服务（先起 API；真实 LLM + 真实 judge）
python scripts/eval_runner.py --base-url http://127.0.0.1:8000 --tag real \
    --judge-base-url https://api.siliconflow.cn/v1 \
    --judge-api-key "$JUDGE_API_KEY" --judge-model Qwen/Qwen2.5-72B-Instruct
```

关键参数：`--limit`（条数）、`--ids`（指定用例）、`--timeout`（默认 120s）、`--retries`（默认 2）、
`--reject`（强制走驳回路径，用于验证 replan）、`--approve`（默认自动通过）。

---

## 4. 种子数据硬校验（任务 2）

`scripts/verify_seed.py` 输出的不是「日志」而是**断言**：任一条失败即 exit≠0，可直接接 CI。

### 4.1 三条硬断言

| 断言 | 做法 | 判据 |
|---|---|---|
| **A1 SQL 只能只读** | 9 条 DML/DDL（DELETE / UPDATE / INSERT / DROP / ALTER / TRUNCATE / 多语句 / 注释绕过 / REPLACE）逐条打给 sql-query 工具，另 2 条合法 SELECT 必须放行 | 全部被拒 + `_SpyExecutor` 证明**执行器零调用**（拦截发生在校验层，不是靠 DB 报错） |
| **A2 kb-search 非 mock 命中断货数据** | `build_kb_provider(mock=False)` → `ResilientKbProvider`；把 primary HTTP 打到 `127.0.0.1:9`（必然失败）逼出熔断，走 mall 兜底 | 命中 `SKU-10086`、库存 0、周转 21 天、含「已断货」字样；且 `MockKbProvider` 回归不受影响（mock=true 仍原样返回 mock 片段） |
| **A3 notify 无审批直接拦截** | 不带审批单调用 notify 工具 | 返回 `APPROVAL_MISSING` 且 `mock_messages` **零新增**（证明"没发出去"，而不是"发了再报错"）；补一张 APPROVED 审批后同一调用返回 `SENT` 且 +1 |

### 4.2 断点抽样（B00~B19，共 20 个随机断点）

固定 `--seed 42` 抽 20 个断点，每条校验三件事：

1. **planner enter 恒为 1** —— 恢复不从头重跑（这是"断点续跑"的第一硬指标）；
2. **恢复前后 plan 的 `(seq, name)` 签名一致** —— 计划没被重排/篡改；
3. **`human_gate` enter ≥2 且为偶数** —— 一次挂起 + 一次放行成对出现。

### 4.3 实测

```
26/26 通过 · SEED VERIFIED · exit=0 · 29.5s
（A1/A2/A3 + SEED-1..3 结构校验 + B00~B19 抽样）
```

### 4.4 用法

```bash
python scripts/verify_seed.py                       # 默认：SQLite 夹具 + 20 断点
python scripts/verify_seed.py --dry-run             # 只看清单不执行
python scripts/verify_seed.py --init-mall-fixture   # 建 SQLite 版 mall 夹具（含 SKU-10086 断货）
python scripts/verify_seed.py --mall-dsn "mysql+aiomysql://mall_ro:pwd@127.0.0.1:3306/mall"
python scripts/verify_seed.py --require-redis       # 断点校验改打真实 Redis checkpoint
```

---

## 5. kb-search 适配层（任务 3）

### 5.1 结构

```
build_kb_provider(mock=None)
├── mock=true  → MockKbProvider()                      # 与 W2 完全一致，一字未改
└── mock=false → ResilientKbProvider(
                    primary   = HttpKbProvider(),    # 项目一知识库 HTTP（带 token）
                    fallbacks = [MallKbProvider()],  # 直查 mall 52 万行只读库
                    breaker   = CircuitBreaker(failure_threshold=3, recovery_seconds=30),
                )
```

调用链：`HTTP 主路` → 失败 3 次熔断（`closed → open`）→ 30s 后 `half_open` 试探 → 成功则闭合。
熔断打开期间**直接跳过主路**，省掉 3 次超时等待；兜底链也失败才返回 `degraded=True`（不抛异常，主流程不阻塞）。

### 5.2 兜底为什么能命中「断货」

`MallKbProvider` 不猜表结构：

- 用 `inspect` 探测 `skus` 表的候选列名（`_COLUMN_CANDIDATES`），探测不到就**降级不猜**（宁可返回空，也不拼错列）；
- `_extract_terms()` 抽 SKU 号 + **领域词表**（断货 / 库存 / 周转 / 预算 / 数码配件…）+ 2~6 字中文 token；
- 关键词全落空时 `relaxed=True` 退化为「周转天数 top-N」——这样「库存周转风险」这类长中文查询也能出证据；
- 输出片段形如 `mall:skus:SKU-10086`，库存 ≤0 追加「已断货」，与 LLM 的表述习惯对齐。

### 5.3 安全边界

只执行**参数化 SELECT**，SQL 由适配层拼装，不接受任何外部 SQL 文本；
连接用只读账号（与 sql-query 工具同一套 `SQL_RO_*` 凭证口径）。

---

## 6. 上下文压缩 A/B（任务 4）

### 6.1 设计

| 组 | 变量 | 说明 |
|---|---|---|
| A 对照 | `CompressionConfig.enabled=False` | 历史不摘要、不注入 |
| B 实验 | `CompressionConfig.enabled=True` | 历史压缩为模板摘要后注入，最多 2 层 |

固定项：同一评测子集、同一 LLM（同温度/预算）、同一段历史种子（`--history-rounds 14`）、同一图与工具。
唯一变量是 `ContextService(enabled=...)`，`w4_common.EmbeddedEnv` 的 `compress` 参数就是这个开关。

### 6.2 一个必须说清的口径问题

`plan_context()` 返回的 `before_tokens` 在两组**不同源**：关闭压缩时只统计组件，开启时才把历史消息并入。
直接相减会得出「压缩组压缩前 token 更大」的荒谬结论。因此脚本自己统一口径：

```
before = tokens(components) + tokens(全文历史)      ← 两组完全相同（实测 20895 / 20895）
after  = tokens(components) + tokens(注入的历史)    ← 注入物 = 摘要 or 原文
```

### 6.3 实测（8 样例 ×2 组）

| 指标 | 对照A | 实验B | 差 | 变化率 |
|---|---|---|---|---|
| 压缩前 token（微基准） | 20895.0 | 20895.0 | 0 | 0% |
| 压缩后 token（微基准） | 20895.0 | **5593.5** | -15301.5 | **-73.23%** |
| 压缩率 | 0 | **0.73** | +0.73 | — |
| 数字保留率 | 1.0 | 0.72 | -0.28 | -28% |
| 关键字保留率 | 0.81 | 0.81 | 0 | 0% |
| 单样例端到端 token | 3184.25 | 4383.62 | +1199.37 | **+37.67%** |
| LLM 调用次数均值 | 4.0 | 4.0 | 0 | 0% |
| P95 时延 | 33.0 | 23.0 | -10 | -30.3% |
| 成功率 / 超时率 | 1.0 / 0 | 1.0 / 0 | — | 持平 |
| 关键字召回率 / faithfulness | 0.37 / 0.02 | 0.37 / 0.02 | 0 | 持平（Mock 模板必然） |

### 6.4 结论（4 条，与报告一致）

1. **压缩层本身有效**：同样历史，注入量 20895 → 5594，**-73.2%**；数字保留率 0.72、关键字保留率 0.81
   —— W2 的 `numeric_field_whitelist` 生效，关键数字没有被塌缩掉。
2. **端到端 token 反而 +37.7%，不是压缩失效**：对照组的策略是「完全不注入历史」，实验组是
   「注入压缩后的摘要」。摘要本身要占 token（约 +1200/样例，全部落在 `planner`：1724 → 2924）。
   正确的对比对象是「原文全量注入」，相对它的节省就是第 1 条的 73%。
   **这也说明压缩的收益前提是「历史本来就该进 prompt」**——如果业务压根不需要历史，关掉更省。
3. **质量侧离线不可判**：两组 faithfulness / 召回完全相等，因为 Mock 输出是确定性模板。
   要拿可对外引用的质量结论，必须 `--llm real` + 独立 judge 模型（避免 self-preference bias）。
4. **性能侧无副作用**：LLM 调用次数 4.0 vs 4.0（压缩不额外调模型），P95 33 → 23ms，超时率 0。
   压缩是纯 CPU 的文本处理，不进模型链路。

### 6.5 用法

```bash
python scripts/ab_compression.py --limit 8 --history-rounds 14 --tag full
python scripts/ab_compression.py --llm real --judge-api-key sk-xxx --tag ab_real   # 真实模型
python scripts/ab_compression.py --no-force-compress    # 用 W2 默认阈值（短历史不会触发压缩）
```

> `--force-compress`（默认开）把 `trigger_ratio` 降到 0.05、`message_rounds_threshold` 降到 0、
> `keep_recent_rounds=1`。**不加这个开关，短历史根本触发不到压缩，A/B 两组会完全一样**，
> 实验就退化成"跑了两遍同样的东西"。这一点在报告里也写明，避免读者误以为压缩在生产默认参数下无效。

---

## 7. 联调方案（三档，按依赖从轻到重）

### 档位 1：全离线（默认，零外部依赖）

```bash
python scripts/verify_seed.py            # 26 项断言
python scripts/eval_runner.py --tag smoke --limit 5
python scripts/ab_compression.py --limit 4 --tag smoke
python scripts/demo_three_flows.py
```

特点：SQLite 文件库 + fakeredis + `InMemoryTransport` + `InMemorySaver` + Mock LLM。
**能验**：编排路由、工具契约、SSE 帧序列、审批幂等、断点恢复、取消收口、指标管线。
**不能验**：模型输出质量、真实 token 计费、MySQL 方言、Redis checkpoint 落盘。

### 档位 2：半真实（真 MySQL + 真 Redis，Mock LLM）——推荐的日常联调档

```bash
docker compose up -d mysql redis
cp .env.example .env            # MOCK_EXTERNAL_RAG=true / MOCK_EXTERNAL_EVAL=true / LLM_MOCK=true
alembic upgrade head
python scripts/seed_w3_demo_users.py
python -m app.main              # 或 uvicorn app.main:app --port 8000

# 另开一个终端
python scripts/verify_seed.py --require-redis            # 断点改打真实 Redis
python scripts/eval_runner.py --base-url http://127.0.0.1:8000 --tag mysql_mockllm --limit 10
python scripts/demo_three_flows.py --live                # 三条链路打真实服务
```

**验收点**：`/api/v1/tasks/{id}/snapshot` 的 `next_nodes` 在恢复前后一致；
`CHECKPOINTER_BACKEND=redis` 时进程重启后仍能 `resume`。

### 档位 3：全真实（真 LLM + 真知识库 + 真 mall 库）

```bash
# .env
LLM_MOCK=false
LLM_API_KEY=sk-xxx
MOCK_EXTERNAL_RAG=false
KB_BASE_URL=http://rag.internal/api/v1        # 项目一知识库
KB_TOKEN=xxx
MALL_RO_DSN=mysql+aiomysql://mall_ro:pwd@10.x.x.x:3306/mall?charset=utf8mb4
JUDGE_BASE_URL=https://api.siliconflow.cn/v1  # 评审模型必须与生成模型不同
JUDGE_API_KEY=sk-yyy
JUDGE_MODEL=Qwen/Qwen2.5-72B-Instruct

python scripts/verify_seed.py --mall-dsn "$MALL_RO_DSN"   # A2 改打真实 mall
python scripts/eval_runner.py --base-url http://127.0.0.1:8000 --tag real
python scripts/ab_compression.py --llm real --tag ab_real --limit 30 --history-rounds 30
```

**验收点**：GMV 幅度准确率应显著高于离线的 0%；faithfulness 的 `judge_mode` 显示 `llm` 而非 `local_rule(no_key)`。

### 7.1 联调常见坑（都是本项目踩过的）

| 现象 | 原因 | 处置 |
|---|---|---|
| `cannot commit transaction - SQL statements in progress` | 单连接内存 SQLite + 并发写 | 用文件库 + WAL + `busy_timeout=30000` |
| `UNIQUE constraint failed: users.username` | 同一库重复 start 重插种子 | 种子账号先 `select` 再插（已修） |
| 脚本连不上库 | `settings.database_url` 由 `MYSQL_*` 派生，**没有 `DATABASE_URL` 环境变量** | 改 `MYSQL_*`，或像 `w4_common` 那样自建 engine |
| A/B 两组指标完全一样 | 短历史不触发压缩 | 加 `--force-compress` 或加大 `--history-rounds` |
| 审批后 resume 报 404/重复审批单 | `human_gate` 整节点重跑 | 建单必须幂等（W3 已实现），脚本侧带 `approval_id` |
| SSE 收不到事件 | 订阅晚于任务结束 | 事件总线是 Redis Stream，用 `last_event_id=0-0` 从头重放 |

---

## 8. Demo 三条链路（任务 6）

```
链路 1  提交高风险任务 → SSE 第一段（收尾 interrupt）→ 审批待办 → resume(APPROVED)
        → SSE 第二段（收尾 complete）→ COMPLETED → 轨迹回放
链路 2  提交高风险任务 → 审批驳回 → replan 重排 → 再走 notify 仍无审批 → FAILED(APPROVAL_MISSING, replan_count=1)
链路 3  任务卡在审批点 → cancel → CANCELLED → 审批单 EXPIRED → pending_interrupts 清空
        → 再 resume 409 → 轨迹与断点时间轴仍可回放
```

实测（`--tag offline`）：

| 链路 | task_id | 终态 | 步骤 | 耗时 |
|---|---|---|---|---|
| 1 审批闭环 | 39 | COMPLETED | 8/8 | 558 ms |
| 2 驳回重规划 | 40 | FAILED | 5/5 | 609 ms |
| 3 取消冻结 | 41 | CANCELLED | 8/8 | 269 ms |

关键证据：

- 链路 1 第一段 5 帧（`state_update×4 + interrupt×1`），第二段 16 帧（`state_update×11 + tool_call×4 + complete×1`），
  节点序列 `planner → human_gate → executor → … → notifier → summarizer`，20 步骤 / 8 断点；
- 链路 2 轨迹含 `replan enter=1`、`notifier enter=1`，最终 `APPROVAL_MISSING`；
- 链路 3 取消前 `interrupt_count=1 / next=['human_gate'] / approval=PENDING`，
  取消后 `interrupt_count=0 / plan 仍在（4 个子任务）/ approval=EXPIRED`，
  再 resume 返回 `HTTP 409 TASK_NOT_PAUSED`。

**SSE 两段式**是这条链路的技术要点：审批挂起时流会以 `interrupt` 收尾（前端据此关连接去走审批），
审批后带 `Last-Event-ID` 续订，一直到 `complete`。脚本用的就是前端同一套语义，
所以它不是"读日志"，而是**复现了真实客户端的订阅行为**。

---

## 9. 验收证据汇总

| 项 | 命令 | 结果 |
|---|---|---|
| 回归测试 | `pytest` | **150 passed**，0 failed，9.39s |
| 种子校验 | `python scripts/verify_seed.py` | **26/26**，`SEED VERIFIED`，exit=0，29.5s |
| 评测跑批 | `python scripts/eval_runner.py --tag full` | 30/30 完成，成功率 100%，产物 4 份 |
| A/B 对照 | `python scripts/ab_compression.py --tag full` | 8×2 组，压缩率 73.2%，报告 1 份 |
| Demo | `python scripts/demo_three_flows.py` | **3/3 链路、21/21 步骤** |

---

## 10. 遗留 P2 清单

按任务书要求「MCP HTTP、FR-014 分层记忆属 P2，仅注释占位，不编码」，本次**未实现**，代码里保留占位与注释：

| # | 项 | 现状 | 为什么留到 P2 | 建议 |
|---|---|---|---|---|
| P2-1 | **MCP HTTP 传输** | 只有 stdio + `InMemoryTransport`；`mcp_client.py` 留了传输枚举与 TODO 注释 | 需先确定网关鉴权与背压策略；stdio 已满足单机吞吐 | 接 Streamable HTTP，工具池按传输类型分组，保留 stdio 兼容 |
| P2-2 | **FR-014 分层记忆（short/mid/long）** | `memory_service.py` 只有短期（会话内）能力，`MemoryScope` 枚举已定义 | 长期记忆涉及写入策略、遗忘曲线、跨租户隔离，需单独设计评审 | 先做 mid（任务级归档）→ 再做 long（向量库 + TTL） |
| P2-3 | MCP Server 横向扩容 | 进程内池，单进程单池 | 依赖 P2-1 | 与 P2-1 一起做 |
| P2-4 | 真实 RAG / Eval 全链路 | 适配层与熔断已就绪，但只在 mock / 本地夹具上验证过 | 项目一接口未稳定 | 接口冻结后跑 §7 档位 3 验收 |
| P2-5 | faithfulness 走项目二 RAGAS | `eval_gateway.py` 已封装 `create_run/get_run`，默认 `MOCK_EXTERNAL_EVAL=true` | 需要独立评测集群 | 先小流量灰度，失败自动降级本地规则（已实现） |
| P2-6 | 评测集扩充 | 30 条（L1~L6） | 覆盖度够验证管线，不够做统计显著性 | 每层扩到 30+，加「对抗样本」层 |
| P2-7 | 前端图表/导出 | MVP 4 个页面，无导出 | 非核心链路 | 报告 PDF 导出、指标看板 |
| P2-8 | 多租户与配额 | 单租户 | 需要产品定义 | 按 `tenant_id` 隔离 checkpoint 与事件流 |

> 这些项在代码中均已标注 `P2` / `TODO(P2)` 注释，不构成隐藏技术债。

---

## 11. 已知限制（结论外推的边界）

1. **离线质量指标不可用**：Mock LLM 输出是确定性模板，GMV 幅度 / 必提词 / faithfulness 在离线档位一律不可引用；
2. **token 是估算值**：`utils/tokens.py` 按 4 字符≈1 token 口径估算，同口径可比、绝对值不可外推计费；
3. **历史种子是构造数据**（同一段事实重复 14 轮），真实会话的压缩率会低于此处的「重复度红利」；
4. **压缩只作用于历史消息这一路**，工具结果瘦身（`SlimmingConfig`）未纳入本次对照；
5. **嵌入式 harness 的基建与生产不同**（SQLite / fakeredis / InMemorySaver / InMemoryTransport），
   行为可验收、性能不可外推（毫秒级数字不代表生产时延）。

---

## 12. 快速开始

```bash
# 0) 依赖（已有 .venv 可跳过）
pip install -r requirements.txt -r requirements-dev.txt

# 1) 五分钟拿到全部证据
pytest -q
python scripts/verify_seed.py
python scripts/eval_runner.py --tag full
python scripts/ab_compression.py --tag full
python scripts/demo_three_flows.py

# 2) 看结果
cat eval_output/eval_report_full.md
cat eval_output/AB实验报告_full.md
cat eval_output/Demo三条链路_offline.md
```

目录结构与项目总览见 `W1-W4项目总览与总结.md`；逐项核对见 `W4-自测核对清单.md`；
A/B 报告的空模板见 `AB实验报告模板.md`。

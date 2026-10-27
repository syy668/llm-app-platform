# AgentFlow W4 评测报告

- 生成时间：2026-10-21T11:26:53
- 评测集：`eval_datasets\分层难例评测集-manifest.json`（30 条）
- 运行目标：embedded(sqlite+fakeredis+InMemorySaver)
- LLM 模式：mock；judge 模式：local_rule(no_key)
- 超时/重试：timeout=120.0s，retries=2

> 离线（LLM_MOCK=true）时生成侧输出是确定性 Mock 文本，**质量类指标只能验证管线是否通**，
> 不能代表真实模型效果；token 类指标同样来自估算器。真实结论请用 `--base-url` + 真实 LLM 复跑。

## 1. 总览

| 指标 | 值 |
| --- | --- |
| 任务成功率 | 100.00%（30/30） |
| 超时率 | 0.00%（0 条） |
| GMV 结论准确率（四项全过） | 0.00%（0/30） |
| GMV 分项：趋势 / 幅度 / 必提词 / 无禁用论断 | 93.3% / 0.0% / 13.3% / 100.0% |
| 重规划次数（均值/总/涉及样例） | 0.0 / 0 / 0 |
| 总 token / 单样例均值 | 98942 / 3298.07 |
| LLM 调用次数（均值） | 125 / 4.17 |
| P50 / P95 时延 | 433.0 ms / 721.0 ms |
| faithfulness 均值 | 0.31（30 条有效） |
| 上下文召回率均值 | 0.35 |

## 2. 分节点 token 消耗（均值）

| 节点 | 均值 | 合计 |
| --- | --- | --- |
| executor | 485.8 | 14574 |
| notifier | 0.0 | 0 |
| planner | 1749.8 | 52494 |
| reviewer | 0.0 | 0 |
| summarizer | 1062.47 | 31874 |

## 3. 分层表现

| 分层 | count | success_rate | gmv_accuracy | latency_p95 | token_avg |
| --- | --- | --- | --- | --- | --- |
| L1-单因归因 | 8 | 1.0 | 0.0 | 497.0 | 3184.25 |
| L2-双因归因 | 6 | 1.0 | 0.0 | 445.0 | 3185.33 |
| L3-高危需审批 | 6 | 1.0 | 0.0 | 721.0 | 3340.0 |
| L4-知识库依赖 | 4 | 1.0 | 0.0 | 471.0 | 3763.25 |
| L5-噪声异常 | 3 | 1.0 | 0.0 | 433.0 | 3185.33 |
| L6-边界 | 3 | 1.0 | 0.0 | 1153.0 | 3235.67 |

## 4. 逐样例明细

| id | status | reason | replan | token | ms | recall | gmv | faith |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AF-EVAL-001 | COMPLETED | COMPLETED | 0 | 3192 | 497 | 0.3333 | ❌ | 0.3636 |
| AF-EVAL-002 | COMPLETED | COMPLETED | 0 | 3192 | 223 | 0.5 | ❌ | 0.2727 |
| AF-EVAL-003 | COMPLETED | COMPLETED | 0 | 3178 | 425 | 0.6667 | ❌ | 0.3636 |
| AF-EVAL-004 | COMPLETED | COMPLETED | 0 | 3184 | 409 | 0.3333 | ❌ | 0.2727 |
| AF-EVAL-005 | COMPLETED | COMPLETED | 0 | 3178 | 222 | 0.3333 | ❌ | 0.2727 |
| AF-EVAL-006 | COMPLETED | COMPLETED | 0 | 3192 | 458 | 0.0 | ❌ | 0.2727 |
| AF-EVAL-007 | COMPLETED | COMPLETED | 0 | 3184 | 420 | 0.5 | ❌ | 0.2727 |
| AF-EVAL-008 | COMPLETED | COMPLETED | 0 | 3174 | 457 | 0.3333 | ❌ | 0.2727 |
| AF-EVAL-009 | COMPLETED | COMPLETED | 0 | 3184 | 437 | 0.6667 | ❌ | 0.3636 |
| AF-EVAL-010 | COMPLETED | COMPLETED | 0 | 3194 | 235 | 0.3333 | ❌ | 0.1818 |
| AF-EVAL-011 | COMPLETED | COMPLETED | 0 | 3186 | 216 | 0.6667 | ❌ | 0.2727 |
| AF-EVAL-012 | COMPLETED | COMPLETED | 0 | 3186 | 445 | 0.3333 | ❌ | 0.1818 |
| AF-EVAL-013 | COMPLETED | COMPLETED | 0 | 3188 | 423 | 0.0 | ❌ | 0.1818 |
| AF-EVAL-014 | COMPLETED | COMPLETED | 0 | 3174 | 231 | 0.3333 | ❌ | 0.2727 |
| AF-EVAL-015 | COMPLETED | COMPLETED | 0 | 3254 | 692 | 0.5 | ❌ | 0.2727 |
| AF-EVAL-016 | COMPLETED | COMPLETED | 0 | 3832 | 687 | 0.6667 | ❌ | 0.4545 |
| AF-EVAL-017 | COMPLETED | COMPLETED | 0 | 3258 | 714 | 1.0 | ❌ | 0.4545 |
| AF-EVAL-018 | COMPLETED | COMPLETED | 0 | 3260 | 721 | 0.0 | ❌ | 0.2727 |
| AF-EVAL-019 | COMPLETED | COMPLETED | 0 | 3248 | 711 | 0.0 | ❌ | 0.1818 |
| AF-EVAL-020 | COMPLETED | COMPLETED | 0 | 3188 | 568 | 0.5 | ❌ | 0.2727 |
| AF-EVAL-021 | COMPLETED | COMPLETED | 0 | 3773 | 471 | 0.3333 | ❌ | 0.4545 |
| AF-EVAL-022 | COMPLETED | COMPLETED | 0 | 3760 | 460 | 0.3333 | ❌ | 0.4545 |
| AF-EVAL-023 | COMPLETED | COMPLETED | 0 | 3752 | 413 | 0.3333 | ❌ | 0.4545 |
| AF-EVAL-024 | COMPLETED | COMPLETED | 0 | 3768 | 461 | 0.3333 | ❌ | 0.4545 |
| AF-EVAL-025 | COMPLETED | COMPLETED | 0 | 3200 | 242 | 0.0 | ❌ | 0.3636 |
| AF-EVAL-026 | COMPLETED | COMPLETED | 0 | 3178 | 421 | 0.5 | ❌ | 0.2727 |
| AF-EVAL-027 | COMPLETED | COMPLETED | 0 | 3178 | 433 | 0.3333 | ❌ | 0.2727 |
| AF-EVAL-028 | COMPLETED | COMPLETED | 0 | 3172 | 409 | 0.3333 | ❌ | 0.3636 |
| AF-EVAL-029 | COMPLETED | COMPLETED | 0 | 3337 | 1153 | 0.0 | ❌ | 0.2727 |
| AF-EVAL-030 | COMPLETED | COMPLETED | 0 | 3198 | 425 | 0.0 | ❌ | 0.1818 |

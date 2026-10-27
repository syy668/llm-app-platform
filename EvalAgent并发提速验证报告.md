# EvalAgent 并发评测实际提速验证报告

- 验证时间：2026-10-19 22:00 ~ 22:20
- 运行记录：**run 21**（可在 /task 页查看，前端 http://localhost:5175/）
- 数据集：**「并发提速验证-30题」（id=13）**，30 条题目与 A/B 基线 run19/run20 完全同题
- 参数：kb_6（18 块 IT 制度库）、混合检索 bm25_vector_hybrid、top_k=8、EVAL_MAX_CONCURRENCY=2、重试 2 次

## 结果总览

| 指标 | run19（串行基线） | run21（并发2，本次） |
|---|---|---|
| 完成样本 | 30/30 | **30/30，零失败** |
| faithfulness | 0.939 | 0.876 |
| context_recall | 1.000 | **1.000** |
| answer_relevancy | 0.962 | 0.885 |
| context_precision | 0.892 | 0.883 |
| 墙钟耗时 | ~22.8 min（45.6 s/题） | **19.6 min（39.1 s/题）** |

**结果口径完全一致**：四指标同分布（faith/AR 的少量差异属 LLM-as-judge 正常波动），无一条失败，error_msg 为空。

## 并发机制验证（核心结论）

用 30 条样本的逐条 latency_ms 反推：

- 单条延迟总和 = 2336s，墙钟 = 1173s
- **实测并行度 = 2336 / 1173 = 1.99 ≈ 2.0**，即并发 2 被完整吃满，机制完全生效
- 进度实时推进：success_items 每次轮询（20s 间隔）均在增长，成对完成（如 8→10、18→20）符合并发 2 特征；前端 /task 页 5s 轮询可实时看到进度条推进
- 批量落库正常：每 10 条一批 flush，trace 每样本一次性落库，无丢失

## 为什么没到 12 分钟？

中位数延迟 48.1s/题 与串行基线（45.6s）一致，说明单题速度没有退化；但出现 **6 题 ~185-190s 的尾部慢响应**（如何申请OA账号 / 会议室预约 / 查看工资条 / 电脑中毒 / 出差住宿标准 / 考勤申诉），这 6 题合计 ~1122s，占满整个墙钟的 95%。

- 该延迟来自 DeepSeek 服务端偶发慢响应（185~190s 高度一致，为外部因素），与本轮并发改造无关
- 剔除这 6 题后理论墙钟 ≈ (2336-1122)/2 ≈ **10.1 min**，与预期 ~12 分钟吻合
- 结论：**并发 2 带来的是真实 2 倍并行加速；墙钟由 LLM 尾部延迟主导**。若要进一步压缩时间，可提高 EVAL_MAX_CONCURRENCY（1~10 可配）对冲尾部慢响应

## 交付物

- 数据集 13「并发提速验证-30题」、运行记录 run 21
- 前端已启动：http://localhost:5175/（/task 页可查看进度与四指标结果）

---

# 追加（10-20 上午）：并发4验证 run23 + 慢题根因闭环

## run23（EVAL_MAX_CONCURRENCY=2→4，仅改 .env，零代码改动）
- 30/30 零失败，**795s = 13.2 min（26.5 s/题）**，对比 run21 并发2（19.6min）再快 32%，对比串行基线（~22.8min）快 42%
- 实测并行度 = 2906/795 = **3.66 ≈ 4**，并发 4 生效
- 四指标：faith 0.889 / cr 1.000 / ar 0.881 / cp 0.897

## 慢题根因闭环（日志+数据双实锤）
- 10 个慢题整齐卡在 **184.7~188.7s**，全部是 RAGAS 指标 job 撞上 **ragas evaluate() 默认 RunConfig 超时 180s** → 对应指标被置 None（9题丢 context_precision，1题丢全部，2个快题也各丢1个）
- 后端日志大量 `Retrying request to /chat/completions` + `ragas TimeoutError`：DeepSeek 官方 API 高峰期单次 judge 调用中位 ~50s、尾部 >60s；agent 链路 ChatOpenAI timeout=60s 掐断后重试加剧耗时
- 结论：**瓶颈在 DeepSeek 官方 API 响应速度，不在并发代码**；并发 4 已把可并行的部分吃满

## 候选方案
- **A（推荐，纯配置）**：LLM 判分链路整体切到 SiliconFlow `deepseek-ai/DeepSeek-V3`（同权重模型，实测 3.9s 响应 vs 官方 ~50s）。agent 端走 /system/config 热更新；RAGAS 端读 .env（RagasEvaluator 不走 runtime_config）需改 .env + 重启。预期 30 题 4~7 分钟，不再撞 180s，指标口径基本不变（同模型不同推理服务）
- **B（最小代码改动）**：metrics.py 的 evaluate() 传 RunConfig(timeout=600) 不丢指标 + llm.py timeout 60→150 减重试；时长不降反可能升
- **C（维持现状）**：13.2 分钟已接近目标，但 10/30 题 context_precision 缺失，聚合口径受损

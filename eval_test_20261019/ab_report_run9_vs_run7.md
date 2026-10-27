# EvalAgent A/B 对比实验报告

> 生成时间：2026/10/19 17:04:40（由 EvalAgent 前端自动生成，计算过程不依赖后端）

## 1. 实验配置说明

| 项目 | A 组 | B 组 |
| --- | --- | --- |
| 评测任务 ID | 9 | 7 |
| 数据集 | IT-TEST-20261019-AB数据集（ID=7） | 同左（前端已校验同源数据集） |
| 评测模型 | deepseek-chat | deepseek-chat |
| RAG 实验参数（本地记录） | chunk_size=默认, search_mode=默认, top_k=3 | chunk_size=默认, search_mode=默认, top_k=8 |
| 样本总数 | 5（成功 3） | 5（成功 3） |
| 创建时间 | 2026-10-19T16:26:51 | 2026-10-19T16:26:51 |

## 2. 汇总指标对比

| 指标 | A 组平均 | B 组平均 | 差值（B - A） | 结论 |
| --- | --- | --- | --- | --- |
| 忠实度 faithfulness | 0.6074 | 0.9256 | +0.3182 | B 显著更优 |
| 答案相关性 answer_relevancy | 0.0828 | 0.1437 | +0.0609 | 略有差异 |
| 上下文精确率 context_precision | 0.0000 | 0.0000 | 0.0000 | 基本持平 |
| 上下文召回率 context_recall | 0.0000 | 0.0000 | 0.0000 | 基本持平 |

## 3. 差异样本列表（任一指标 |Δ| > 0.1，共 1 条）

| # | question | 指标 | A 分数 | B 分数 | Δ（B-A） |
| --- | --- | --- | --- | --- | --- |
| 1 | 如何查看工资条 | faithfulness | 0.0000 | 0.9000 | +0.9000 |

## 4. 样本级明细（对齐后前 50 条）

| # | question | A faithfulness | A context_recall | B faithfulness | B context_recall |
| --- | --- | --- | --- | --- | --- |
（浏览器实测捕获：对齐样本 3/3，faithfulness A=0.6074 / B=0.9256，Δfaith +0.3182、Δrelevancy +0.0609；差异样本 +0.9000 标黄）

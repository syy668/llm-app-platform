# EvalAgent 评测中台 —— 产品与工程落地总结

> 一份面向面试的"项目速记手册"与简历素材。全部结论基于当前代码库真实实现，每条关键结论均标注可翻源码的路径。

---

## 一、真实应用背景与解决的问题

### 1. 为什么要把"评测"从知识库项目里拆出来，单独做中台？

最直接的动因是**算力隔离防雪崩**与**统一裁判尺度**，但更深一层是"裁判与被测不能同床"。

**痛点现象**：项目一（企业垂直知识库 Agent）最初把 RAGAS 评测做在自己进程内，结果是一旦评测和线上问答抢同一个 LLM/向量/数据库，跑批慢时会反过来拖垮线上问答；而且"自己评自己"——用同一个模型既生成答案又给答案打分，天然有自评偏差，分数不可信。

**根因**：评测的负载特征（长时间、高并发、大量 token 消耗）和线上问答（低延迟、突发）完全不同，混在一起谁都不讨好。

**代码级方案**：把评测拆成独立服务 EvalAgent（FastAPI 独立后端 + 独立 MySQL 库 `eval_agent_db`），与被测系统（项目一 8010）通过 HTTP 解耦。裁判模型（考官）与被测模型（考生）走两套独立配置：
- 考官（RAGAS 打分）：`app/services/judge.py`，独立 LLM + 独立 embedding
- 考生（被评测的问答模型）：`app/services/rag_client.py`，直连项目一 8010 取数

**为什么"考官"和"考生"要分开配**（`app/services/runtime_config.py`）：如果两者是同一个模型，评判标准会跟着被测系统一起漂，A/B 对比就没有意义。这就是 `JUDGE_*` 和 `UPSTREAM_RAG_*` 两套配置并行存在的根本原因。

### 2. 它在 AgentFlow 生态中的角色

一句话：**项目三（AgentFlow）产出数据，项目二（EvalAgent）当裁判打分。**

- 项目三 AgentFlow 是多智能体任务编排平台，跑出来的 RAG 问答结果需要客观评价；
- 项目二 EvalAgent 是中央评测中台，接收项目三推送的数据（或直接读项目一的知识库样本），用 RAGAS 四项指标统一打分，产出可跨系统比较的分数。

代码层面，接口做了**双挂载兼容**（`app/core/config.py:23`）：`/eval/*` 与 `/api/v1/eval/*` 同时可用，适配前端 5174 和项目三两套调用习惯；序列化层还专门留了 `__compat__` 字段兼容项目三的 `_poll_upstream` 轮询（`app/services/serialize.py:89`）。

---

## 二、系统全貌与核心功能（大白话版）

### 1. 一次评测任务的完整流转链路

把系统想象成一场"**统一阅卷考试**"：

1. **出卷**（前端）：用户在数据集管理页上传/选中题目集（`DatasetView.vue`），点"创建评测任务"。
2. **报名登记**（后端）：`POST /eval/runs` 在 `eval_runs` 表登记一条任务，状态 `pending`，立刻返回"已在后台跑"（`app/api/v1/runs.py`）。
3. **考前检查考官**（探活）：`execute_run` 第一件事就是 `judge.preflight()`——考官（裁判模型）先试答一道结构化题，答不出 JSON 就直接判任务失败，不浪费后面任何一次取数调用（`app/services/runner.py:180`）。
4. **考生答卷**（取数）：并发调项目一 8010 的问答接口，把每个题目让"考生"（被评测的 RAG 系统）答一遍，受 `UPSTREAM_RAG_CONCURRENCY` 约束（`app/services/rag_client.py`）。
5. **阅卷打分**（RAGAS）：把（题目、考生答案、召回片段、标准答案）喂给 RAGAS 的四个指标——faithfulness / answer_relevancy / context_precision / context_recall，逐条算分（`app/services/judge.py`）。
6. **登分**（落库）：每算完一条就 `commit` 一条 `eval_scores`，前端进度条实时往前走（`app/services/runner.py:196`）。
7. **发榜**（前端）：任务详情页展示四项指标均值、逐条明细、BadCase 红绿灯（`TaskView.vue`）。

### 2. 核心功能清单

| 功能 | 说明 | 关键实现 |
|---|---|---|
| **评测执行** | 异步并发打分，支持项目三推数据 | `runner.py` 的 `Semaphore(RUN_ITEM_CONCURRENCY)` 控制并发 |
| **A/B 对比** | 双雷达图 + 指标差值 | 前端 `AbCompareView.vue` + 后端 `POST /eval/runs/compare` |
| **大盘看板** | 成功率、平均指标、Token 消耗 | `TaskView.vue` 的 KPI 卡片 + `/eval/runs/stats` |
| **任务管控** | 查看进度、中途停止（Cancel） | `runner.py` 的取消标记轮询 + 前端红色"停止"按钮 |
| **系统配置** | 考生/考官双面板分离、保存即探活 | `SettingsView.vue` + `preflight` 探活机制 |

---

## 三、开发中遇到的真问题及解决（深水区排雷）

这是整个项目最有含金量的部分。我按"痛点现象 → 根因排查 → 代码级方案"逐个还原。

### 深水区 1：裁判模型"默认思考"，指标静默为空 + 烧钱 3~4 倍

**痛点现象**：换用 `deepseek-flash` 后，faithfulness 大量样本是空值（NaN），而且跑一轮 7 条要多花好几块钱。

**根因排查**：实测发现 `deepseek-flash` 是推理模型，默认开启思考链（reasoning）。它把答案写进 `reasoning_content`，而 `content` 字段是空的——RAGAS 只读 `content`，自然解析不到 JSON。更糟的是 reasoning token 按**输出价**计费（是输入价 4 倍），且解析失败又触发重试，恶性循环。

**代码级方案**（`app/services/judge.py:234`）：关思考的参数**不同厂商名字不一样**，必须按域名区分：

```python
base = (cfg.api_base or "").lower()
if "deepseek.com" in base:
    extra_body["thinking"] = {"type": "disabled"}   # DeepSeek 官方
else:
    extra_body["enable_thinking"] = False           # 阿里百炼 dashscope
```

之前只传 `enable_thinking=false` 对 DeepSeek 官方是**被静默忽略的**——这就是为什么"看起来关了其实没关"。

### 深水区 2：RAGAS 解析失败把 StringIO 当结果返回

**痛点现象**：某条样本 faithfulness 一直是空，还白白烧了 78 次 judge_calls、67 秒。

**根因排查**：读 ragas 0.2.6 源码 `prompt/pydantic_prompt.py:388` 发现一个上游缺陷——JSON 解析失败后，它调 `fix_output_format_prompt` 让模型"修格式"，但**把返回的 StringIO 对象直接当结果返回，没有重新 parse**。上层取 `.statements` 时直接 `AttributeError: 'StringIO' object has no attribute 'statements'`，被 ragas 吞成 NaN。

**代码级方案**（`app/services/judge.py:53`）：运行时打补丁，monkeypatch `RagasOutputParser.parse_output_string`，在 `fix` 之后**把修好的字符串重新 parse 成目标模型**，真的还是坏才抛异常。

### 深水区 3：英文切句器把中文 markdown 答案切成 0 句

**痛点现象**："答案明明答了、知识库也有资料，为什么 faithfulness 是空？"

**根因排查**：faithfulness 分两步——先按句拆成陈述，再逐条判有无上下文支撑。但 ragas 默认的切句器是英文 pysbd，而且只保留**以 `. 。 ! ！` 结尾**的片段。中文 markdown 答案（表格行 `| 满1年 | 5天 |`、加粗标题 `**密码要求**`）被英文切句器切出来的片段全是换行/竖线结尾 → **命中 0 句** → 陈述列表空 → NLI 无话可判 → NaN。实测 7 条里 3 条这么空掉。

**代码级方案**（`app/services/judge.py:113`）：自研 `ChineseMarkdownSegmenter`——先去 markdown 装饰，再按中英文句末标点+换行切句，给无终止符的片段补「。」，保证每句都能进 NLI。挂到指标上由 `JUDGE_CN_SEGMENTER` 开关控制。

### 深水区 4：代理劫持，本地调用也走代理导致打分挂死

**痛点现象**：打分偶发 `APIConnectionError`、超时 300s，看起来像"样本太难"或"模型不兼容"。

**根因排查**：本机存在 `HTTP(S)_PROXY` 环境变量，httpx 默认 `trust_env=True`，会把**连 `127.0.0.1:8010` 的本地请求也塞进代理**绕一圈。抖动时本地被测系统调用直接失败。

**代码级方案**（`app/core/config.py:49` + `app/services/judge.py:251`）：`HTTP_TRUST_ENV=False`，所有裁判/上游/鉴权的 httpx 客户端显式 `trust_env=False` 直连。

### 深水区 5：RAGAS 并发单例竞态（指标模块级单例被互相覆盖）

**痛点现象**：并发打分时，某条样本的指标偶发拿到 `None` 的 llm/embeddings，报"set LLM before use"。

**根因**：ragas 的指标（faithfulness 等）是**模块级单例**，多线程并发 evaluate 会互相覆盖彼此的 `llm`/`embeddings` 属性。

**代码级方案**（`app/services/judge.py:442`）：每次打分前在 `_metric_lock` 锁内显式回填指标属性；`score()` 先取本地引用再传，避免跨在 `reload()` 中间拿到 `None`。

---

## 四、硬核工程化落地

### 1. 高可用与防呆

- **探活机制（preflight）**（`app/services/judge.py:317`）：保存配置前先做"结构探活"——既要连通、又要能按 RAGAS 要的 JSON 结构输出、JSON 里还要有 0~1 的数值字段，三档判定，不通过就不落库。向量模型也单独探活（`_probe_embeddings`）：能出向量、维度一致、**不是全零**（个别网关会对不认识的模型返回 `[[0.0]*dim]` 这种"成功但无意义"的响应，会让 answer_relevancy 恒为 0，比报错更难查）。
- **热更新**：配置写库后调 `judge.reload()` 丢弃旧客户端，下次打分按最新配置重建，无需重启（`app/services/judge.py:308`）。
- **温度锁死 0.0**（`app/services/judge.py:262`）：为保证评测可复现，裁判温度强制锁定 0，且前端界面上直接禁用、标注"已锁定"。

### 2. 并发与容错

- **快进快出（异步）**：任务通过 `asyncio` 后台执行，`Semaphore` 控制并发（打分 `RUN_ITEM_CONCURRENCY=3`、取数 `UPSTREAM_RAG_CONCURRENCY=4`），逐条提交让进度实时可见（`app/services/runner.py:201`）。
- **致命错误熔断**（`app/services/runner.py:236` + `:331`）：遇到配置性致命错误（`AllocationQuota` 额度耗尽、`invalid_api_key`、`NotFoundError` 等）**立刻终止整轮**，不无脑重试空转，写清根因。这是从"连续 3 条空指标就熔断"（`RUN_EMPTY_METRIC_STREAK_LIMIT=3`）升级来的。
- **缺失指标留痕**（`app/services/runner.py:467` + `:564`）：单指标 NaN 定向补跑一次（`JUDGE_MISSING_METRIC_RETRY`），补跑仍空则写 `error_msg="指标缺失：xxx"`；聚合时输出 `missing_rates`，缺失率超 20% 告警（`METRIC_MISSING_WARN_RATE`）。**关键教训：看均值前先看空值率，否则空值被剔除会让均值假高。**

### 3. 前端体验

- **大盘 KPI 卡片**：成功率、平均 faithfulness、累计 Token 消耗一屏可见（`TaskView.vue`）。
- **停止按钮的交互**：执行中/排队中的任务，删除按钮替换成红色"停止"，带二次确认，防跑飞止损（`TaskView.vue:546`）。
- **指标红绿灯**：BadCase 阈值默认 0.6，低于 0.6 标红（`BADCASE_DEFAULT_THRESHOLD=0.6`）；A/B 差值超 0.1 标黄（`COMPARE_DIFF_THRESHOLD=0.1`，前端 `ea-diff-row`）。

---

## 五、项目局限性与演进

### 1. 代码难以完全解决的口径问题

- **没有 Ground Truth 时 Recall 算不出**：context_precision / context_recall 依赖标准答案（reference），拒答型样本（标准答案就是"知识库里没有"）根本没法算 recall——这是指标定义本身的局限，不是 bug。
- **拒答样本拉低 AR**：answer_relevancy 是"由答案反推问题再比相似度"，对"正确地拒绝回答"这种答案，反推必然跑偏。代码做了 `REFUSAL_SKIP_AR`（拒答样本 AR 不计入聚合），但这是"绕开"而非"解决"。
- **换裁判 = 换尺子**：不同裁判模型打出的分数不可直接跨轮比较（实测 qwen3.6-flash 和 qwen3.8-max 的 AR 能差 0.3）。代码用 run 快照 `evaluator_config.judge_model` 留痕追溯，但没法消除模型间的系统性偏差。
- **指标本身有天花板**：faithfulness 对"算出来的数值"（如表格提取的 43.3%）不给分，这是 RAGAS 的 NLI 判定局限。

### 2. v2.0 演进方向

- **统一的裁判基线**：把裁判模型固定到某个稳定型号，建立"黄金基线"（golden baseline），新任务先和基线对齐再比较。
- **多裁判交叉验证**：用多个异构裁判模型打分取中位数，降低单模型偏差。
- **细粒度归因**：从"faithfulness 低"下沉到"哪条陈述被判了不忠实"，直接定位是召回问题还是生成问题。
- **成本可见性**：把 `JUDGE_PRICE_PER_1M` 配好，让每次评测的 token 成本自动折算成金额展示。

---

## 附：面试绝杀话术

> **面试官问：如何在分布式架构下，保证 AI 评测的客观性与稳定性？**
>
> 我会分三层回答，这正好是我们踩过坑后沉淀下来的设计。
>
> **第一层，客观性靠"考官考生分离 + 温度锁死 + 换裁判留痕"。** 评测最大的敌人是"自评自判"——用同一个模型既生成答案又打分，分数天然不可信。所以我们把裁判（考官）和被评测模型（考生）彻底解耦，走两套独立配置；裁判温度强制锁 0 保证可复现；每次 run 都存下 `evaluator_config.judge_model` 快照，明确告诉用户"换裁判就是换尺子"，跨轮比较先核对快照。
>
> **第二层，稳定性靠"保存即探活 + 致命错误熔断 + 缺失留痕"。** 我们做了三层防线：配置保存前先结构探活，模型吐不出 RAGAS 要的 JSON 就根本不让落库，把故障挡在第一个样本之前；跑批中遇到额度耗尽、key 失效这类配置性致命错误，立刻熔断整轮而不是无脑重试空转；单指标偶发 NaN 定向补跑一次，补不回来就显式写进 `error_msg`，绝不静默——因为静默的空值会把均值"虚高"，比报错更危险。
>
> **第三层，最难的是"看起来正常其实坏了"的隐蔽故障。** 我们实测踩过几类：推理模型默认思考导致 content 空、英文切句器把中文答案切 0 句、网关对不认识的向量模型返回全零向量。这类问题的共同点是"不报错，只静默产出垃圾分"。解法是**在探活和落库两个关口都做语义校验**——不只查"通不通"，还查"返回的 JSON 里有没有 0~1 的数值""向量是不是全零""答案是不是被切成了 0 句"。客观评测的稳定性，本质是对"假成功"的持续对抗。

---

### 关键源码索引（面试翻源码速查）

| 能力 | 文件 |
|---|---|
| 裁判打分 + 探活 + 关思考 | `app/services/judge.py` |
| 任务调度 + 并发 + 熔断 + 补跑 | `app/services/runner.py` |
| 配置热更新 + 考官考生分离 | `app/services/runtime_config.py` |
| 取数（连项目一 8010） | `app/services/rag_client.py` |
| 聚合 + 缺失率 | `app/services/aggregate.py` |
| 配置项全集 | `app/core/config.py` |
| 鉴权（调上游 /auth/me） | `app/core/auth.py` |
| A/B 对比前端 | `evalagent_vue/src/views/AbCompareView.vue` |
| 任务大盘前端 | `evalagent_vue/src/views/TaskView.vue` |
| 系统配置前端 | `evalagent_vue/src/views/SettingsView.vue` |

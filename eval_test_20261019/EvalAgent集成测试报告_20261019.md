# 企业垂直知识库 Agent 后台（eval 模块）× EvalAgent-vue 前端 集成测试报告

- **测试日期**：2026-10-19 16:19 ~ 17:15
- **被测对象**：
  - 后端：企业垂直知识库Agent后台（eval 模块增量增强），http://127.0.0.1:8000
  - 前端：EvalAgent-vue 独立前端工作台，http://localhost:5174
- **测试类型**：后端+前端集成测试（手工用例 + API 自动化驱动 + 浏览器端到端）
- **测试账号**：admin/admin123456（管理员）；user01/user123456（普通用户，测试前通过 POST /users 预置创建——环境中原本不存在 user01，已按测试方案补建）
- **测试资源**：kb_id=6（企业运维知识库样例，已向量化，对应方案的"kb-id=1 可正常 RAG 问答"；环境中不存在 kb_id=1，已用实际可用知识库等价替代）；valid_30.csv（30条）、empty.csv、bad_header.csv、over_max_rows.csv(501行)、binary.exe（预置文件缺失，测试现场生成等价文件）
- **评测模型**：deepseek-chat（真实 LLM 调用）

---

## 一、手工测试用例表

| TC编号 | 测试模块 | 测试步骤 | 预期结果 | 实际结果 | 测试状态 | 备注 |
|---|---|---|---|---|---|---|
| TC1-1 | eval接口-rag_config | 不传 rag_config，POST /eval/runs 创建任务（5条数据集） | 200 创建成功，rag_config=null | HTTP 200，run=6，详情/列表 rag_config=null | PASS | 落库为 JSON null 字面量（见 BUG-4 备注），API 层返回 null |
| TC1-2 | eval接口-rag_config | 仅传 {top_k:8} 创建任务 | 200 创建成功，rag_config={top_k:8} | HTTP 200，run=7，查询返回 {"top_k": 8} | PASS | top_k 为唯一请求级生效参数（系统约束） |
| TC1-3 | eval接口-rag_config | 完整传 {chunk_size:500, search_mode:vector_only, top_k:8} | 200 创建成功，三字段完整落库；查询期仅 top_k 生效 | HTTP 200，run=8，DB 直查 rag_config='{"top_k": 8, "chunk_size": 500, "search_mode": "vector_only"}' | PASS | chunk_size/search_mode 仅落库展示属**已知系统约束**，不算缺陷 |
| TC1-4a | eval接口-参数边界 | top_k=0 创建任务 | 422 校验错误（范围1-50） | HTTP 422 "Input should be greater than or equal to 1" | PASS | 错误信息明确指出字段与约束 |
| TC1-4b | eval接口-参数边界 | top_k=100 创建任务 | 422 校验错误（范围1-50） | HTTP 422 "Input should be less than or equal to 50" | PASS | — |
| TC1-5 | eval接口-枚举校验 | search_mode="hybrid_bogus" 创建任务 | 422 枚举校验错误 | HTTP 422 "String should match pattern '^(vector_only\|bm25_vector_hybrid)$'" | PASS | — |
| TC1-6 | eval接口-数值校验 | chunk_size=-100 创建任务 | 422 数值校验错误（100-4000） | HTTP 422 "greater than or equal to 100" | PASS | — |
| TC1-7a | eval接口-查询 | GET /eval/runs/{id}（仅top_k任务）详情 | rag_config 正确返回 {top_k:8} | HTTP 200，rag_config={"top_k": 8} | PASS | — |
| TC1-7b | eval接口-查询 | GET /eval/runs/{id}（完整配置任务）详情 | 三字段 JSON 完整返回 | HTTP 200，三字段完整 | PASS | — |
| TC1-7c | eval接口-查询 | GET /eval/runs/{id}（未传配置）详情 | rag_config=null | HTTP 200，rag_config=null | PASS | — |
| TC1-7d | eval接口-查询 | GET /eval/runs 列表 | 列表逐条正确返回 rag_config（JSON/null） | HTTP 200，三种形态均正确 | PASS | — |
| TC2-1 | 删除级联-dataset | 管理员 DELETE /eval/datasets/{id}（含2样本+1条done任务），直查MySQL | dataset_items 级联删除(2→0)；eval_runs 保留 | items 2→0；run_rows=1 保留、scores=1 保留、GET run 200 | PASS | 二次级联测试实证；phase2 首轮读数为事务隔离误读，已用 autocommit 复核 |
| TC2-1b | 删除级联-dataset | 删除后查询 eval_runs 中该 dataset 的任务记录 | 历史评测记录保留不删除 | run 保留且可查询 | PASS | 与代码注释"级联删样本，保留运行记录"一致 |
| TC2-1c | 删除级联-重复删除 | 对已删除资源再次 DELETE | 404 | dataset/run 均 HTTP 404 | PASS | — |
| TC2-2 | 删除级联-run | 管理员 DELETE /eval/runs/{id}，直查MySQL | eval_run_scores 级联删除；dataset/items 不受影响 | run15 scores 1→0；删除响应含 deleted_scores 计数；其他数据集 items 30→30 | PASS | — |
| TC2-3 | 删除级联-权限 | 普通用户 user01 调用 DELETE dataset/run | 403 权限拒绝 | 均 HTTP 403 "仅管理员可执行该操作" | PASS | — |
| TC3-1 | 权限-未登录 | 不带 Token GET /eval/datasets | 401 未授权 | HTTP 401 "未登录或登录已过期" | PASS | — |
| TC3-2 | 权限-普通用户可读 | user01 GET /eval/datasets、GET /eval/runs | 200 可读 | 均 HTTP 200（datasets=5条/runs=5条） | PASS | GET 放开登录可读的增量变更生效 |
| TC3-3 | 权限-普通用户写 | user01 POST /eval/runs | 403 禁止 | HTTP 403 | PASS | — |
| TC3-4 | 权限-普通用户删除 | user01 DELETE dataset/run | 403 禁止 | 均 HTTP 403 | PASS | 权限校验先于资源存在性校验，符合安全惯例 |
| TC3-5 | 权限-管理员全通 | admin 执行创建数据集/导入样本/创建任务/删除数据集/删除任务/导出 | 全部 200 正常 | 全链路 200（含 UI 与 API 双通道） | PASS | — |
| TC4-1 | 异常输入 | dataset_id=999999 创建任务 | 404 | HTTP 404 "评测数据集不存在" | PASS | — |
| TC4-2 | 异常-空数据集 | 对 0 条样本数据集创建任务，等待终态 | 后台任务状态 failed | run=10 终态 **done（0/0）**，metrics 四项为 null，error_msg=null | **FAIL** | 见 BUG-1；未走 failed 分支 |
| TC4-3 | 幂等补列 | 重复启动服务，验证不重复执行 ALTER TABLE 报错 | 启动正常，无重复补列报错 | 代码审查：inspector 先查列存在即 return + 异常兜底不阻塞启动；DB 确认列存在（json NULL） | **BLOCK** | 重启服务实测未执行（避免中断正在执行的评测任务与用户在用服务）；静态证据充分，建议上线前自测一次重启 |
| TC5-1 | 前端E2E-全链路 | UI 上传 valid_30.csv→建数据集（30条解析成功）→UI 创建任务（rag_config top_k=8 完整配置）→等待 done→详情→BadCase 筛选→导出 CSV | 全链路贯通 | 上传解析"已解析 30 条有效样本"→run14 创建成功→终态 done 24/30（faithfulness 0.7974）→详情/BadCase 正常→导出接口 200（BOM+表头+24条数据） | PASS | 无头浏览器本地下载保存验证受限，导出已用同接口直接验证（227KB CSV，BOM 正确） |
| TC5-2 | 前端-配置展示 | 任务列表、详情页展示后端返回的 rag_config | 列表与详情均展示 | 任务列表"RAG 实验配置"列正确展示（含"默认"回退与"未记录"文案）；**详情抽屉未展示 rag_config** | **FAIL** | 见 BUG-2（低危） |
| TC5-3 | 前端E2E-A/B看板 | ①同数据集跑 runA(top_k=3)/runB(top_k=8) ②A/B页选择两个 done 任务 ③question 对齐+图表+差值>0.1标黄 ④导出 Markdown 含两份 rag_config | 端到端可用 | runA=#9/runB=#7；开始对比后：对齐样本 3/3、ECharts 柱状图渲染（A 0.607/B 0.926）、Δfaith +0.3182、差异样本 +0.9000 单元格标黄（rgb(255,251,230)）、Markdown 报告含两份 RAG 实验参数 | PASS | 汇总指标表的差值列无标黄（仅样本对比表标黄），与文案"任一指标 \|Δ\|>0.1 标黄"基本一致，建议统一（见 BUG-5 备注） |
| TC5-4a | 前端-异常CSV | 上传 empty.csv（空文件） | 前端拦截提示，不调用后端 | 提示"CSV 文件为空"，无后端请求 | PASS | 校验在文件选择阶段（on-change）执行，先于 API 调用（csv.ts:59） |
| TC5-4b | 前端-异常CSV | 上传 bad_header.csv（表头"问题,答案"） | 前端拦截提示，不调用后端 | 提示"表头必须为 question,ground_truth（当前为：问题,答案）"，无后端请求 | PASS | — |
| TC5-4c | 前端-异常CSV | 上传 over_max_rows.csv（501行） | 前端拦截提示，不调用后端 | 提示"样本数 501 超过单数据集上限 500 条"，无后端请求 | PASS | — |
| TC5-4d | 前端-异常CSV | 上传 binary.exe（非csv恶意文件） | 前端拦截提示，不调用后端 | 提示"仅支持 .csv 文件（UTF-8 编码…）"，无后端请求 | PASS | — |
| TC5-5 | 前端-普通用户只读 | user01 登录：浏览数据集/任务/A-B看板；检查创建/删除按钮 | 列表可读；创建、删除按钮置灰不可点击 | 三个页面均可读；数据集页仅剩"预览样本"按钮、任务页仅剩"详情"按钮，创建/删除/导出入口**整体隐藏**（等效不可用，比置灰更严格） | PASS | 实现方式为隐藏而非置灰，效果等价，产品上可二选一（备注） |
| TC5-6 | 前端-删除确认 | 管理员点删除→检查二次确认弹窗→先取消→再确认删除→观察列表 | 有确认弹窗；取消不删；确认后列表刷新 | 弹窗标题"删除确认"，文案含级联说明（"将级联删除其 N 条样本；已生成的评测任务记录会保留"）；取消后数据集仍在；确认后行消失且后端已删除 | PASS | — |
| TC6-1 | 兼容性-旧数据 | GET /eval/runs/1~4（历史无 rag_config 任务）+ 前端展示 | API 返回 null；前端显示"未设置（沿用知识库配置）" | API 四条均 rag_config=null；前端显示"未记录（沿用知识库默认配置）" | PASS | 前端文案与方案措辞略异，语义一致 |
| TC6-2 | 兼容性-无配置新建 | 不带 rag_config 新建 run，验证 DB 与评测行为 | DB rag_config 为 null，评测用知识库原生 top_k | run=6 正常执行（done 3/5），API 返回 null；DB 存 JSON null 字面量（非 SQL NULL） | PASS | JSON null 与 SQL NULL 的差异见 BUG-4（低危提示） |

---

## 二、测试统计汇总

| 指标 | 数量 |
|---|---|
| **总用例数** | **35** |
| **PASS** | **32** |
| **FAIL** | **2**（TC4-2、TC5-2） |
| **BLOCK** | **1**（TC4-3，重启未实测，静态证据充分） |
| 通过率 | 91.4%（含 BLOCK 口径为 94.3% 可释） |

补充观察（非用例计分）：
- 真实 LLM 评测下部分样本未得分：run11/14 为 24/30，A/B 5条数据集各 run 3/5。经 trace 表核实，执行窗口内 **0 条 error**，未得分样本是路由判为 `direct`（未走 RAG）被静默跳过（见 BUG-5）。
- Chroma 偶发 `Could not connect to tenant default_tenant` 导致个别样本检索失败后模型回答"未找到相关信息"（环境稳定性问题，建议关注 Chroma 服务健康检查）。

---

## 三、缺陷清单

| 缺陷ID | 模块 | 现象 | 复现步骤 | 严重等级 | 分类 |
|---|---|---|---|---|---|
| BUG-1 | 后端 eval 执行器 | 空数据集（0条样本）创建评测任务，终态为 `done（0/0）`、指标全 null，而非预期的 `failed` | 创建不含任何样本的数据集 → 对其创建评测任务 → 等待终态 → GET /eval/runs/{id} | **中** | 真实BUG（eval_service.py:342 无空样本守卫，直接落 RUN_DONE） |
| BUG-2 | 前端任务详情 | 详情抽屉不展示 rag_config（任务列表已展示），不满足"列表、详情页面展示"的验收预期 | 打开任意任务"详情"抽屉 → 查看头部信息（仅模型/样本数/四项指标，无 RAG 实验参数） | **低** | 真实BUG（TaskView.vue:373-448 抽屉模板缺 rag_config 字段） |
| BUG-3 | 前端创建任务弹窗 | 数据集下拉中部分数据集样本数显示为 0（如"#5 11（0 条）"），但 API item_count=20、列表页显示 20 | 打开"创建评测任务"弹窗 → 展开数据集下拉 → 对比数据集管理页样本数 | **低** | 真实BUG（待核实渲染字段来源，疑似取 items 关联长度而非 item_count） |
| BUG-4 | 后端数据一致性 | 新建 run 不传 rag_config 时落库为 JSON `null` 字面量，历史补列数据为 SQL NULL，两种"空"并存 | 分别查询 run1（SQL NULL）与 run6（JSON null）：`SELECT rag_config, JSON_TYPE(rag_config) FROM eval_runs` | **低** | 真实BUG（隐患型：`WHERE rag_config IS NULL` 会漏掉 JSON null 行，建议统一写入 SQL NULL） |
| BUG-5 | 后端 eval 执行器（设计局限） | 路由判为 direct 的样本被静默跳过：不写分、不计数、无错误标记，前端进度显示为"未成功"（如 24/30），运营会误判为"评测失败" | 任一 30 条数据集执行评测 → 对比 eval_run_scores 行数与 total_items → 查 trace_logs 确认 direct 路由 | **中** | 真实BUG/设计局限（eval_service.py:310-311 `continue` 无 skipped 计数；建议增加 skipped_items 字段并在前端区分"跳过(未走RAG)"与"失败"） |
| — | rag_config 生效范围 | chunk_size/search_mode 查询期不生效，仅持久化展示；仅 top_k 请求级透传生效 | — | — | **已知系统约束**（按验收要求不计缺陷；A/B 报告中 search_mode/chunk_size 仅作记录对照，不构成检索差异变量） |
| — | 环境稳定性 | 偶发 Chroma "Could not connect to tenant default_tenant"（该样本回答退化为"未找到相关信息"） | 重复执行评测观察个别样本 | **低** | 环境问题（非本次增量引入，建议为知识库服务加健康检查与重试） |

---

## 四、验收测试结论

**结论：有条件通过（可进入上线评审，建议先修复 BUG-1、BUG-5）**

达成项：
1. **rag_config 全生命周期正确**：创建（三态：不传/部分/完整）→ 422 边界校验（top_k 0/100、search_mode 非法枚举、chunk_size 负值）→ 落库（MySQL JSON 列）→ 查询返回（列表+详情 API）→ 前端展示，全链路验证通过；
2. **权限模型符合设计**：GET 登录可读、写/删仅管理员（401/403 边界全部正确）；
3. **删除级联符合设计**：删 dataset → 样本级联删、评测记录保留；删 run → 得分级联删；重复删除 404；
4. **前端 E2E 关键链路可用**：CSV 上传解析、异常文件四类前端拦截（0 后端调用）、任务创建、进度跟踪、BadCase 筛选、CSV 导出（BOM 正确）、A/B 看板（question 对齐、ECharts、|Δ|>0.1 标黄、Markdown 报告含双 rag_config）、删除二次确认、普通用户只读；
5. **旧数据兼容**：历史 run rag_config=null 正确返回与展示；无配置新任务走知识库原生配置正常执行。

遗留问题（上线前建议处理）：
- BUG-1（中）：空数据集任务应落 failed 并给出明确 error_msg；
- BUG-5（中）：增加 skipped_items 口径，避免 direct 跳过污染"成功率"与 A/B 结论；
- BUG-2/3/4（低）：详情抽屉补 rag_config 展示、下拉样本数字段修正、rag_config 空值统一为 SQL NULL；
- TC4-3 重启幂等未实测（BLOCK）：建议发布窗口执行一次服务重启并观察启动日志确认无 ALTER 报错。

遗留风险：
- A/B 对比的**可信样本量**受 direct 跳过影响（本次 5 条仅 3 条可对齐），演示前建议确保数据集问题能稳定命中 rag 路由；
- 评测耗时约 35-40 秒/条（deepseek-chat），30 条约 20 分钟，演示时建议使用 5-10 条小数据集或预热好的历史 run。

---

## 五、上线/演示注意事项

1. **【关键】chunk_size / search_mode 仅落库展示，查询期不生效**：
   - 只有 `top_k` 会真正透传到问答检索链路（run_agent 请求级 options 白名单仅支持 top_k、route_enabled）；
   - `chunk_size` 是入库切片参数，查询期无法生效；`search_mode` 只持久化存入 eval_runs 表供 A/B 报告展示，**不会传给 retriever**；
   - **演示、面试、README 必须说明此约束**，避免观众/面试官追问"为什么改了 search_mode 结果没变化"。推荐话术："rag_config 是实验参数记录 + top_k 请求级生效的设计：top_k 即时生效可做 A/B；chunk_size 属于入库期参数，改动需要重新向量化，当前版本仅做实验记录；search_mode 预留了字段与展示，检索模式切换在后续版本接入。"
2. **A/B 看板演示选型**：两个 run 必须来自同一数据集（前端强校验），且尽量选 `rag` 路由命中率高的问题集；建议用 top_k=3 vs top_k=8 这种能稳定拉开 faithfulness 差异的组合（本次实测 +0.32）。
3. **导出权限**：CSV 导出仅管理员可用（403），user01 演示时不要点导出；前端已对普通用户隐藏相应入口。
4. **普通用户权限展示**：前端采用"隐藏按钮"而非"置灰"实现只读，验收口径需与产品确认一致；user01 的删除/创建请求即使绕过 UI 也会被后端 403 拦截，安全无风险。
5. **空数据集/失败任务**：演示前检查任务列表避免出现 0/0 的空 run（BUG-1 修复前建议在创建任务前端校验样本数>0）。
6. **重启发布**：服务启动有幂等补列逻辑（代码已兜底），MySQL 已人工执行 ALTER TABLE；发布重启后建议 grep 日志确认无 "补列 rag_config 失败" 告警。
7. **数据清理**：本次测试产生的 IT-TEST-*/IT-UI-*/IT-CASCADE-* 前缀数据集与 run（2、4、5 号为更早冒烟数据）保留在库中供对照，如需清理请按"先删 run 后删 dataset"的顺序操作。

---

## 附：测试证据文件清单（E:/AI_Project_Workspace/eval_test_20261019/）

| 文件 | 说明 |
|---|---|
| results1.json / results2.json / phase3.json | 三轮自动化断言原始结果 |
| states.json | 各评测任务终态与指标快照 |
| export_run14.csv | run14 导出 CSV（227KB，BOM 正确，24 条得分） |
| ab_report_run9_vs_run7.md | A/B Markdown 报告实测捕获重建 |
| ab_dashboard.png / ab_samples.png | A/B 看板渲染截图 |
| valid_30.csv / empty.csv / bad_header.csv / over_max_rows.csv / binary.exe | 测试夹具 |

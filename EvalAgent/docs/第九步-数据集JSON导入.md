# 第九步：数据集上传支持 JSON（+ 追加样本）

> 需求一：数据集管理页原来只收 CSV（固定表头 `question,ground_truth`），需要支持 JSON。
> 结论：**后端零改动**，只改前端解析层 —— 因为 `ItemCreate` 本来就支持
> `reference_contexts` / `stratum` / `difficulty` / `item_type`，只是 CSV 表达不了这些列，一直没用上。
>
> 需求二（同日补做）：给已有数据集加「追加样本」按钮。这次**后端补了一道总量闸**，见第八节。

---

## 一、为什么 JSON 不是"多支持一个后缀"

CSV 只有三列（`question` / `ground_truth` / 可选 `stratum`），而后端样本模型有 10 个字段。
JSON 一进来，这些字段终于能用上：

| 字段 | CSV | JSON | 作用 |
|---|---|---|---|
| `question` | ✅ 第 1 列 | ✅ | 必填 |
| `ground_truth` → `reference_answer` | ✅ 第 2 列 | ✅ | 必填（缺它 `context_recall` 永远算不出） |
| `stratum` | ✅ 第 3 列 | ✅ | 分层难例标签，A/B 分层看板依赖它 |
| `reference_contexts` | ❌ | ✅ | 标准上下文，未来算 context 相似度类指标要用 |
| `difficulty` | ❌ | ✅ | 1~5 难度，配合 `stratum` 做难度分析 |
| `item_type` | ❌ | ✅ | `rag_qa` / `tool_call` / `multi_turn` / `agent_task` |

另外：项目一、项目三导出的评测集**本来就是 JSON 数组**，先手工转 CSV 再上传是多余的一道活。

---

## 二、支持的格式（四种都收）

### 1. 数组（最标准）

```json
[
  {
    "question": "2026 年 Q3 华东区销售额是多少？",
    "ground_truth": "2026 年第三季度华东区销售额为 1200 万元",
    "stratum": "easy",
    "difficulty": 1,
    "reference_contexts": ["2026 年第三季度华东区销售额为 1200 万元，同比增长 18%"]
  }
]
```

### 2. 带包装的对象（`items` / `samples` / `data` / `list` / `rows` / `records` 任一）

```json
{ "name": "黄金集 v1", "items": [ { "question": "...", "ground_truth": "..." } ] }
```

> 兼容这个形态是因为很多平台导出时会把元信息（name / version / created_at）包在外面。

### 3. 单个样本对象

```json
{ "question": "...", "ground_truth": "..." }
```

### 4. JSONL / NDJSON（一行一个对象，适合几千条）

```
{"question":"Q1","ground_truth":"A1"}
{"question":"Q2","ground_truth":"A2"}
```

> 附带兜底：把 JSONL 内容存成 `.json` 也能识别（解析失败时自动按逐行 JSON 再试一次，并提示"已按 JSONL 解析"）。

### 字段别名（各家导出的表头五花八门，且大小写/下划线/驼峰混杂）

| 目标字段 | 认的写法 |
|---|---|
| `question` | `question` / `query` / `q` / **问题** / **提问** |
| `ground_truth` | `ground_truth` / `groundTruth` / `reference_answer` / `referenceAnswer` / `answer` / `gold` / **标准答案** / **参考答案** / **答案** |
| `reference_contexts` | `reference_contexts` / `contexts` / `gold_contexts` / **参考上下文** |
| `stratum` | `stratum` / `layer` / `category` / **分层** / **类别** |
| `difficulty` | `difficulty` / **难度** |
| `item_type` | `item_type` / `type` / **样本类型** |

匹配时把 key 统一 `toLowerCase()` 并去掉 `_` / `-` / 空格，所以 `Reference Answer`、`referenceAnswer`、`reference_answer` 等价。

---

## 三、校验口径（与 CSV 保持一致，不搞两套标准）

| 情况 | 处理 | 理由 |
|---|---|---|
| `question` 为空 | **拒绝该条** | 没问题的样本无法评测 |
| `ground_truth` 为空 | **拒绝该条** | 缺参照 → `context_recall` 永久算不出，属于"能导入但评不了"的坑 |
| `stratum` > 64 字符 | **拒绝该条** | 后端 `max_length=64` 会 422，前置拦截给友好文案 |
| `difficulty` 不在 1~5 | 忽略该字段 + 提示 | 难度不影响能否评测，没必要整条拒绝 |
| `item_type` 非法 | 忽略该字段 + 提示 | 后端默认 `rag_qa`，忽略即可 |
| 总条数 > 500 | **整体拒绝** | 与后端 `ItemsImport(max_length=500)` 对齐 |
| 报错条数 > 50 | 截断为前 50 条 + "共 N 处问题" | 500 行全错时不要把界面刷屏 |

「阻断错误」与「提示」分成两个数组：`errors`（有一条就不让导入）/ `warnings`（只提示）。
原来的实现把两者混在一个数组里，导致"某条 difficulty 越界"会让整份文件判为不通过 —— 那是我这轮顺手修掉的语义错误。

---

## 四、改动清单（全在前端）

| 文件 | 改动 |
|---|---|
| `src/utils/datasetFile.ts` | **新增**：`parseDatasetFile()` 统一入口 + `parseDatasetJson()` + `parseDatasetJsonl()` + 字段别名归一 + 错误/提示分离 |
| `src/utils/csv.ts` | 未改（CSV 仍是单一事实来源，被 `datasetFile` 复用） |
| `src/views/DatasetView.vue` | 上传项改「数据文件」、`accept=".csv,.json,.jsonl,.ndjson"`、显示当前文件名、格式示例可折叠展开、错误/提示分区展示、删除确认补 `try/catch`（原来取消删除会 Unhandled Rejection） |
| `src/api/types.ts` | `ItemInput` 补可选 `difficulty` / `item_type`（JSON 透传，Payload 类型才诚实） |
| `evalagent_vue/scripts/verify_dataset_parser.ts` | **新增**：13 个解析用例（CSV 兼容 / 四种 JSON 形态 / 中文字段 / 别名 / 各类拒绝），不依赖测试框架 |

**后端一行没改** —— `ItemCreate` 早已支持这些字段，`POST /eval/datasets/{id}/items` 原样接收。

---

## 五、验证

### 1. 解析器用例（13/13）

```bash
# 项目里没有前端测试框架，用 esbuild 打成一个 cjs 直接跑断言
cd EvalAgent/evalagent_vue
node node_modules/esbuild/bin/esbuild scripts/verify_dataset_parser.ts \
  --bundle --platform=node --format=cjs --outfile=scripts/.verify_dataset_parser.cjs --alias:@=./src
node scripts/.verify_dataset_parser.cjs
```

```
ok    CSV 兼容            ok    JSON items 包装      ok    中文字段名
ok    JSON 数组           ok    单对象                ok    驼峰+全量字段
ok    缺 ground_truth 拒绝  ok  非法 item_type 仅提示  ok    JSONL
ok    JSONL 存成 .json 自动兜底  ok  坏 JSON 报错  ok  不支持的扩展名  ok  空数组

全部 13 个用例通过
```

### 2. 真实 UI 端到端（puppeteer 塞文件、点创建、回查数据库）

```
已生成 JSON 文件（3 条，含 stratum/difficulty/reference_contexts）
含 JSON 示例 ground_truth： true      含 reference_contexts 说明： true
UI 已确认解析出 3 条有效样本
UI 已确认：创建成功，已导入 3 条样本
后端数据集：#303 JSON 导入验证-140307 item_count=3
  seq=1 question=2026 年 Q3 华东区销… stratum=easy difficulty=1 contexts=1
  seq=2 question=哪些 SKU 的退货率超过 … stratum=hard difficulty=4 contexts=0
  seq=3 question=上周广告投放的 ROI 是多… stratum=—    difficulty=— contexts=0
扩展字段落库校验通过（stratum / difficulty / reference_contexts）
已清理测试数据集：{"deleted_dataset_id":303,"deleted_items":3}
```

注意 `seq=3` 那条没写 `stratum` / `difficulty` → 落库是 `NULL` / `NULL`，
**不是空字符串**：可选字段"没提供"与"提供了空值"要能区分开，否则分层看板会多出一个空的层。

### 3. 类型检查

```
node node_modules/vue-tsc/bin/vue-tsc.js --noEmit   →  exit 0
```

---

## 六、截图

| 文件 | 内容 |
|---|---|
| `docs/evidence/21-stage9-json-parsed.png` | 上传 JSON 后：`已解析 3 条有效样本` + 「支持的两种格式」展开（CSV / JSON 示例并排） |
| `docs/evidence/22-stage9-json-created.png` | 创建成功：`数据集「JSON 导入验证-140307」创建成功，已导入 3 条样本`，表单已重置 |

---

## 七、没做 / 边界

1. **不支持 Excel / YAML**：xlsx 需要额外解析库（体积 + 版本维护），且评测集本质是纯文本问答，
   CSV/JSON 已覆盖；真需要就让业务方导出 CSV。
2. **`meta` 字段未透传**：后端 `ItemCreate.meta` 接受任意 JSON，但前端没解析——没有明确用途前不猜字段语义。
3. **CSV 与 JSON 的必填口径一致**（都要 ground_truth）。如果确实要做"只有问题、没有标准答案"的评测集，
   需要两边一起放宽并同步改 `_finalize` 的 `context_recall` 缺失口径 —— 属于口径变更，不在这次范围内。

---

## 八、追加样本（同日补做）

需求：数据集建好后要能往里加样本。后端 `POST /eval/datasets/{id}/items` **本来就支持**（seq 接续、
`item_count` 累加），缺的是前端入口 + 一道总量闸。

### 1. 两道闸，不能只有一道

| 闸 | 位置 | 拦什么 |
|---|---|---|
| 单次请求 ≤ 500 | `ItemsImport.max_length=500`（pydantic） | 一次传太多 |
| **单数据集总量 ≤ 500** | `settings.DATASET_MAX_ITEMS` → `add_items` 返回 **409** | 反复追加无限撑大 |

第二道是这次新加的。没有它，页面写的"单数据集最多 500 条"就是假承诺，而且一轮评测的裁判开销没有上限。
前端另有一道前置校验（剩余额度），但那只是体验优化 —— **真闸在后端**，因为前端拦不住 curl。

```python
existing = count(EvalItem where dataset_id=...)
if existing + len(payload.items) > settings.DATASET_MAX_ITEMS:
    raise HTTPException(409, detail=f"追加后样本数 {existing + incoming} 超过单数据集上限 ...")
```

### 2. `seq` 必须接续，不能重排

```python
current_max = max(EvalItem.seq where dataset_id=...)
for offset, item in enumerate(payload.items, start=1):
    EvalItem(seq=current_max + offset, ...)
```

原因：`eval_scores.item_seq` 是**样本序号快照**。如果追加时从 1 重排，那么
"历史任务里的第 3 条"和"现在的第 3 条"就是两条不同的样本 —— A/B 逐条对齐会直接错位，
而且这种错位在界面上看起来完全正常（两列数字都有值），属于最难查的一类。

同时 `item_count` 改成 `existing + len(rows)`（用真实计数，不再用 `max_seq + n`）：
一旦以后有"删除单条样本"的功能，max_seq 就不等于条数了，计数会悄悄漂。

### 3. 前端：把文件选择抽成组件

`components/DatasetFileInput.vue`：上传区 + 解析 + 错误/提示 + 额度校验，创建表单与追加弹窗共用。
不抽的话两份 el-upload + 解析逻辑迟早分叉（一边支持 JSON 了另一边没有），这类问题在上一轮
「后端接口有了但前端 0 引用」的假交付教训里已经吃过一次。

追加弹窗的关键信息：

```
当前 2 条 / 上限 500 条 · 本次最多可加 498 条
新样本的序号接着现有最大序号往下排（不重排已有样本）——历史评测明细里的序号是快照，重排会让 A/B 对齐错位。
```

列表页：操作列加「追加样本」；`item_count >= 500` 时按钮禁用 + tooltip「已达上限」；
样本数量列在满额时显示"已满"。

### 4. 测试（5 例）

```
tests/test_stage10_dataset_append.py .....
5 passed
```

| 用例 | 锁什么 |
|---|---|
| `test_append_continues_seq_and_updates_count` | seq 1,2 → 追加 3 条得 3,4,5；老样本原样保留；扩展字段（stratum/difficulty/reference_contexts）透传 |
| `test_append_twice_accumulates` | 连续追加两次，seq 连续不跳号 |
| `test_append_rejects_when_total_would_exceed_cap` | 超上限 409 + 文案带当前/本次/上限；**失败不留半截数据** |
| `test_append_to_missing_dataset_returns_404` | 不存在的评测集 |
| `test_append_accepts_exactly_up_to_cap` | 边界：正好填满必须放行（off-by-one 最容易翻车） |

顺手改了一个旧用例：`test_stage2_smoke.py` 原来用 `== {"imported":2,"item_count":2}` 整体比字典，
新加的 `previous_count` 字段会让它红。改成只断言契约字段 —— 对会演进的接口做整体字典相等，
等于把"加兼容字段"误判成破坏性变更。

### 5. 端到端（真实 UI）

```
已建数据集 #361，现有 2 条
该行按钮： [ '预览样本', '追加样本', '删除' ]
弹窗文案： 当前 2 条 / 上限 500 条 · 本次最多可加 498 条 …
弹窗已确认解析 2 条
UI 已确认：追加 2 条，当前共 4 条
后端样本：seq=1 老样本Q1 / seq=2 老样本Q2 / seq=3 追加Q3(stratum=hard,difficulty=4) / seq=4 追加Q4
seq 接续校验通过（1,2,3,4，老样本未重排）
已清理测试数据集
```

截图：

| 文件 | 内容 |
|---|---|
| `docs/evidence/23-stage10-append-dialog.png` | 追加弹窗初始态（额度提示 + 上传区） |
| `docs/evidence/24-stage10-append-parsed.png` | 塞入 JSON 后：已解析 2 条 / 按钮变「追加 2 条」 |
| `docs/evidence/25-stage10-append-done.png` | 追加成功后：列表里该数据集 2 → 4 条 |

### 6. 仍然没做

1. **不能删除单条样本**（后端也没有该接口）。加了这个功能，`item_count` 与 `seq` 的口径要一起重新想
   （是本轮特意把 `item_count` 改成真实计数的原因）。
2. **追加不影响已有任务的 `total_items`**：那是创建任务时的快照，历史任务的进度/成功率必须按当时的样本数算，
   不能因为数据集后来变长而改变。这个"不一致"是有意为之。
3. 追加的样本不会回填到历史 run 里（历史 run 的得分明细只覆盖它当时的样本）。

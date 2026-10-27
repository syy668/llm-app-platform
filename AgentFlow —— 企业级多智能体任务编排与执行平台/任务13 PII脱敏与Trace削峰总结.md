# 任务 13：PII 脱敏 + Trace 异步削峰（工程补强收官）

> 全量回归 **645 passed / 0 failed**（630 基线 + 15 新增），ruff 全过，`app/core/trace.py`（HTTP trace_id 中间件）零改动。

---

## 任务 1：Trace 落库前 PII 脱敏（合规红线）

### 审计先行：真实落库链路

- `app/core/trace.py` 只是 **HTTP trace_id 中间件**（W1 边界，文件头注释明确不承载落库）；
- 真正入口 = `app/services/trace_service.py::TraceService.record` → `task_executions` 表
  （AgentFlow 的轨迹表是 `task_executions`，字段 `payload` 承载 input/output，非项目一的 `trace_logs`）；
- 既有脱敏：`_safe_payload` 已对 **dict** payload 走 `mask_mapping`（手机号+邮箱+敏感 key 全量打码）。

### 审计出的两个真实缺口（本次补上）

1. **身份证号没覆盖**（PRD NFR-S3 只列了手机/邮箱/token）；
2. **纯字符串 payload 完全没过脱敏**——`_safe_payload` 只对 dict 生效，而 LLM 原文、
   工具结果片段恰恰是最容易携带 PII 的自由文本。

### 改动 diff

**`app/utils/masking.py`（新增身份证正则 + `mask_pii` 纯函数，`mask_text` 收编委托）**

```python
+ _IDCARD_RE = re.compile(
+     r"(?<!\d)(\d{6})(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
+ )
+
+ def mask_idcard(value: str) -> str:
+     """身份证打码：保留前 6 位（地区）与末 4 位，中间 8 位（生日+顺序码）打星。"""
+     return _IDCARD_RE.sub(lambda m: f"{m.group(1)}********{m.group(0)[-4:]}", value)
+
+ def mask_pii(text: str) -> str:
+     """PII 脱敏纯函数（合规红线）：bearer 凭证 / 身份证 / 手机号 / 邮箱。"""
+     value = _BEARER_RE.sub("Bearer ***", text)
+     value = _IDCARD_RE.sub(lambda m: f"{m.group(1)}********{m.group(0)[-4:]}", value)
+     return mask_email(mask_phone(value))

  def mask_text(value: str) -> str:
-     value = _BEARER_RE.sub("Bearer ***", value)
-     return mask_email(mask_phone(value))
+     return mask_pii(value)
```

效果：`13812345678 → 138****5678`；`110101199003077512 → 110101********7512`（生日全遮）。
所有调用 `mask_text` 的地方（trace payload、日志摘要）自动获得身份证脱敏。

**`app/services/trace_service.py::_safe_payload`（补字符串分支）**

```python
  if isinstance(payload, dict):
      masked: Any = mask_mapping(payload)
+ elif isinstance(payload, str):
+     masked = mask_text(payload)
  else:
      masked = payload
  return _shrink(masked, max_chars)
```

脱敏在截断**之前**（既有顺序约束：先截断会把敏感串切一半，正则失效）——
测试 `test_safe_payload_masks_non_dict_scalar_strings` 专锁超长 preview 路径。

---

## 任务 2：Trace 入库异步削峰（防阻塞主流程）

### 架构

```
业务节点 ──record()──▶ 构造行(脱敏/截断) ──put_nowait──▶ asyncio.Queue(10000)
                                                            │
                                        守护协程（懒启动）：攒满 50 条
                                        或首条滞留 2 秒 → executemany 批量写
                                                            │
FastAPI lifespan shutdown ──▶ shutdown_trace_writer() ──▶ flush 尾部 + 停协程
```

### 关键实现（`app/services/trace_service.py`）

- **`_build_row` 纯函数**：同步路径（`TaskExecution(**row)`）与缓冲路径（`insert(TaskExecution), rows` executemany）共用同一份行构造，**两条路的脱敏口径完全一致**；
- **`TraceBuffer`**：
  - `enqueue` 只做 `put_nowait`，永不阻塞执行图；队列满（10000）丢弃 + WARNING（观测不能反向压垮业务）；
  - 守护协程懒启动：攒满 50 条或首条滞留 2 秒即批量写（`asyncio.wait_for` 定时语义）；
  - **在途批次持有在 `self._pending`（`asyncio.Lock` 保护所有权转移）**——这是调试中抓到的第一版设计缺陷：批次放在协程局部变量里，对 `flush()`/`pending` 不可见，stop 时还会丢数据（"数据卡在协程局部变量里的暗仓"）；
  - `stop()` = 排干尾部 + cancel 协程；worker 内写入用 `asyncio.shield` 包裹，cancel 不打断在途 DB 写；
  - 写失败只告警 fail-open（与既有"轨迹写失败不抛出"口径一致）；
- **`shutdown_trace_writer()` 模块级钩子**：遍历 `_ACTIVE_BUFFERS` 统一 stop，挂在 `main.py` lifespan 的 `yield` 之后、`close_redis`/`dispose_engine` 之前——**重启不丢最后一批轨迹**；
- **`w3_config.py` 新增 4 配置**：`TRACE_ASYNC_BUFFER_ENABLED=True`、`TRACE_BUFFER_BATCH_SIZE=50`、`TRACE_BUFFER_FLUSH_INTERVAL=2.0`、`TRACE_BUFFER_MAXSIZE=10000`。

### 测试兼容性（零测试改动）

conftest 新增 autouse fixture：`monkeypatch.setattr(w3, "TRACE_ASYNC_BUFFER_ENABLED", False)`——
e2e 测试统一走同步落库（断言即时可见，确定性 100%）；缓冲本身由 15 条专项单测覆盖
（显式 `buffered=True`）。**全部既有测试零改动通过**。

### 专项测试（`tests/test_task13_trace_pipeline.py`，15 条）

- mask_pii：手机/邮箱/身份证（保留头尾）/bearer/混合文本（普通数字不动）/纯文本零误伤；
- 落库脱敏：字符串 payload（此前缺口）、dict payload + 敏感 key、超长 preview 路径；
- 缓冲：flush 排干、批量触发（batch=3 写 3 余 2）、定时触发（interval=0.1s）、
  stop 幂等 + closed 后忽略、写失败 fail-open、shutdown 钩子全量落库 + 注册表清理、
  `buffered=False` 行为与改造前逐字节一致（主键即时返回）。

---

## 调试花絮（两个值得记的坑）

1. **"数据卡在协程局部变量里的暗仓"**：第一版 worker 用局部 `pending` 列表攒批，
   `flush()` 只排队列 → 攒着的批次 flush 不到、stop 时直接丢。修复：在途批次提升为
   实例状态 `_pending` + `asyncio.Lock` 所有权转移。
2. **`async_sessionmaker` 少一个括号**：`async with self._resolve_factory() as session`
   拿到的是 sessionmaker 本身不是 session（`TypeError: does not support the asynchronous
   context manager protocol`）。fail-open 把它吞成静默 0 行落库——复刻 `_write` 裸抛才现形。

---

## 面试话术：异步批处理削峰 + PII 脱敏

> "这两个改动我都在动手前先做了链路审计。**PII 脱敏**：发现既有机制只覆盖 dict 型 payload 和两类 PII，而最容易带手机号身份证的恰恰是 LLM 原文这种自由文本——所以我不是加一层新脱敏，而是把脱敏收敛成一个 `mask_pii` 纯函数、补上身份证正则和字符串分支，并保证脱敏发生在截断之前（先截断会把敏感串切一半，正则就失效了）。**异步削峰**：业务侧 `put_nowait` 进 `asyncio.Queue` 永不阻塞执行图，守护协程攒满 50 条或滞留 2 秒就用 executemany 批量写，吞吐从 N 次往返变 N/50 次；shutdown 时 lifespan 钩子排干队列不丢尾部数据。两个坑值得讲：一是我把在途批次从协程局部变量提升为锁保护的实例状态，否则 flush 排不到、stop 会丢数据——批处理系统里'数据到底住在哪'必须是一等公民；二是 fail-open 把一个少括号的 TypeError 吞成了静默 0 行落库，观测系统的容错反而会掩盖自己的故障，所以告警里必须带失败原因和条数。合规这边：脱敏口径在同步/异步两条落库路径上共用同一个行构造函数，从结构上杜绝'某条路忘了脱敏'。"

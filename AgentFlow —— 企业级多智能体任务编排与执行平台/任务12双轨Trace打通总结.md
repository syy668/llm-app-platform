# 任务 12：自研 Trace ↔ OpenTelemetry 双轨打通

> 改动原则：不碰 MySQL 落库逻辑（`trace_service.py` 零改动），OTel 全程旁路。全量回归 **630 passed / 0 failed**（623 基线 + 7 新增）。

---

## 一、审计结论：90% 的基建已存在，本轮拦截了重复造轮子

动手前审计发现，W5-Part2 已交付一套**完整的、活的** OTel 桥，用户要求的三项改进的等价映射：

| 任务要求 | 已有实现（无需重建） |
|---|---|
| OTel 依赖注入 | `requirements-observability.txt` 已含 `opentelemetry-api/sdk/exporter-otlp-proto-http`（v1.45.1 已安装），**零新增依赖** |
| `OTEL_ENABLED=False` 开关 | `w6.ENABLE_OTEL: bool = False`（语义相同，默认值相同，`.env` 已配 `ENABLE_OTEL=false`） |
| `OTEL_EXPORTER_ENDPOINT` | `w6.OTEL_EXPORTER_OTLP_ENDPOINT`（默认 `http://127.0.0.1:4318`；4318 是 OTLP/HTTP 标准端口，比任务书写的 4317/gRPC 更贴合本项目的 proto-http exporter） |
| TracerProvider + BatchSpanProcessor 封装 | `app/observability/otel_tracing.py::init_tracing`（含采样率、OTLP 探活、**不可达自动降级 console、连续 3 次导出失败熔断切 console**） |
| Trace 生命周期挂 Hook | `GraphNodeSpanHandler`（`on_chain_start/end` 识别 LangGraph 节点，`task.id`/`graph.goal` 属性）+ Graph/MCP/LLM/SQL 四类 monkey-patch 埋点，`bootstrap.py` lifespan 已挂载 |
| Fail-safe | `span()` 全程 try-except + `contextlib.suppress`；关闭时连 patch 都不打，零开销 |

**为什么不另建 `app/core/otel_bridge.py` + w2 重复开关**：两套开关（w2.OTEL_ENABLED 与 w6.ENABLE_OTEL）会造成配置分叉——运维改了 A，B 依旧生效，这是监控类系统最危险的静默失效。正确姿势是收编既有实现、只补真实缺口。

## 二、本轮真实增量：把"并存"升级为"打通"

双轨制的最后一块拼图是**跨系统互查**：自研 MySQL trace（trace_logs 表）与 OTel span 各用各的 trace_id，出问题时无法从一方定位到另一方。

**`app/observability/otel_tracing.py` 两处注入：**

```python
# ① span() 通用入口：自动注入自研 trace_id（与 MySQL trace_logs 同源的 ContextVar）
attrs = dict(attributes or {})
if "agentflow.trace_id" not in attrs:
    own_trace_id = get_trace_id()
    if own_trace_id:
        attrs["agentflow.trace_id"] = own_trace_id

# ② GraphNodeSpanHandler 节点 span：补 agent.name（= 节点名，即多智能体的 agent 身份）
attrs = {
    "langgraph.node": node,
    "agent.name": node,                      # 语义别名，OTel 后端可按 agent 维度检索
    "task.id": self._task_id,
    "agentflow.trace_id": get_trace_id() or None,   # 双轨关联键
    ...
}
```

**打通后的排查闭环**：在 Jaeger/Tempo 看到可疑 span → 读 `agentflow.trace_id` 属性 → 回 MySQL `trace_logs` 查该任务完整执行轨迹（含 LLM 原文、工具参数）→ 反向亦然。全部 span（HTTP/Graph/MCP/LLM/SQL）统一携带，调用方零改动。

## 三、测试（7 项，`tests/test_task12_otel_bridge.py`）

- **验收 1**：`ENABLE_OTEL=False` → `install_instrumentation()` 返回 False、不 patch、`span()` no-op、零 span；
- **验收 2**：开启后手动 span 与图节点 span 均携带 `task.id` / `agent.name` / `agentflow.trace_id`；路由函数（名称≠节点名）不产 span，`agent.name` 不被污染；`task_id_var`（任务 7 日志用）与 OTel 属性并存互不干扰；
- **验收 3**：**exporter 恒抛异常**（模拟 collector 宕机）→ 业务协程正常返回、节点回调不外泄异常、LLM 调用结果不受影响，且断言 exporter 确实被调过（异常真实发生过，不是测试假通过）。

实测钉死了一个 SDK 行为：`SimpleSpanProcessor.on_end` 自带吞异常（只打日志），叠加桥内 `contextlib.suppress`，构成双层 Fail-safe——未来升级 OTel SDK 若破坏此行为，验收 3 会立刻报警。

## 四、面试绝杀话术

> **"为什么自研 Trace 而不是 LangSmith？你考虑过 OpenTelemetry 吗？"**
>
> "恰恰相反——LangSmith 和 OpenTelemetry 我都认真评估过，我的结论不是'不用标准'，而是**'自研做深度、标准做广度'的双轨制**，而且两条轨是打通的。
>
> **为什么不押注 LangSmith**：它是 SaaS 黑盒——LLM 的完整 Prompt/响应要出我们的内网，成本按量计费且和厂商锁定；更关键的是，多智能体系统里真正要排查的问题一半不在 LLM 层，而在 MCP 工具调用、SQL 闸门、审批断点这些业务环节，LangSmith 看不到这些。自研 Trace 把 trace_id 用 ContextVar 从 HTTP 中间件一路贯穿到每条日志、每次落库，排查时一个 ID 串起全部上下文——这个深度只有自己掌控数据才能做到。
>
> **但我没有闭门造车**：系统里有一套完整的 OpenTelemetry 桥——TracerProvider、BatchSpanProcessor、OTLP 导出、按 Graph/MCP/LLM/SQL 分类的埋点，配置一个开关全部旁路生效，MySQL 落库一行没改。设计上我坚持两条原则：第一，**Fail-safe**，OTel 挂了绝不影响业务——我甚至用测试钉死了 SDK 的异常吞噬行为，exporter 宕机时业务协程零感知；第二，**双轨可互查**——这是我特意补的一块：每个 OTel span 都自动携带我们自研体系的 trace_id 作为关联属性，在 Jaeger 里看到可疑 span，拿这个 ID 就能回 MySQL 查到完整的执行轨迹，包括 LLM 原文和工具参数。监控后端我们随时可以换——今天接 Jaeger，明天接公司的 SkyWalking，协议是业界标准的，数据主权在自己手里。
>
> 一句话总结：**自研不是排斥标准，而是把标准当作导出协议而非存储与口径的权威**——深度归自己，互通归生态。"

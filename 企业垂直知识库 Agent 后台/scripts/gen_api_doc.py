"""从 docs/openapi.json 生成结构化接口清单 docs/接口文档.md（可重复执行）。"""
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = json.load(io.open(ROOT / "docs" / "openapi.json", encoding="utf-8"))
paths = spec["paths"]
schemas = spec.get("components", {}).get("schemas", {})

groups = {}
for path, ops in paths.items():
    for method, op in ops.items():
        if method not in {"get", "post", "put", "delete", "patch"}:
            continue
        tag = (op.get("tags") or ["其他"])[0]
        groups.setdefault(tag, []).append((method.upper(), path, op))

order = ["认证", "用户管理", "知识库", "文档管理", "会话", "问答", "流式问答",
         "报表", "Trace 日志", "RAGAS 评测", "系统", "智能体"]


def sort_key(t):
    for i, o in enumerate(order):
        if o in t:
            return (i, t)
    return (99, t)


lines = []
lines.append("# EvalAgent 接口文档（结构化清单）")
lines.append("")
lines.append("> 由 Swagger OpenAPI 自动导出生成（`python scripts/export_openapi.py` -> `docs/openapi.json`），与本文件保持同源。")
total = sum(len(v) for v in groups.values())
lines.append(f"> 接口总数：{total} 个 | 服务前缀：`/api/v1` | 完整出入参 Schema 见 Swagger UI（`http://localhost:8000/docs`）或 openapi.json。")
lines.append("")
lines.append("## 通用约定")
lines.append("")
lines.append('- **统一响应体**：所有接口返回 `{"code": 0, "message": "ok", "data": ...}`；失败时 code 非 0、data 恒为 null。')
lines.append("- **鉴权方式**：除登录/刷新/健康检查外，均需 `Authorization: Bearer <access_token>`。")
lines.append("- **双 Token**：access_token 30 分钟（Redis session，滑动过期），refresh_token 7 天（一次性轮换 + 登出拉黑）。")
lines.append("- **错误码口径**：400 业务校验失败；401 未登录/令牌失效；403 已登录但权限不足；404 资源不存在；422 参数校验失败；500 服务器内部错误。所有错误响应头均携带 `X-Trace-ID`（与服务端日志 trace_id 对应）。")
lines.append("- **Trace 链路**：网关/前端可传 `X-Request-ID`，后端复用为全局 TraceID；未传则自动生成，回写于响应头 `X-Trace-ID`，全部日志自动携带。")
lines.append("")
for tag in sorted(groups, key=sort_key):
    lines.append(f"## {tag}")
    lines.append("")
    lines.append("| 方法 | 路径 | 说明 | 权限 | 错误码 |")
    lines.append("|---|---|---|---|---|")
    for method, path, op in groups[tag]:
        desc_lines = (op.get("description") or "").strip().splitlines()
        first_desc = desc_lines[0] if desc_lines else ""
        # 优先取 docstring 首行（中文说明），老路由无 docstring 时回退函数名
        desc = (first_desc or op.get("summary") or "-").replace("|", "\\|")[:80]
        full = (op.get("description") or "")
        perm = "登录" if op.get("security") else "公开"
        if "管理员" in full or "仅管理员" in full:
            perm = "管理员"
        codes = sorted(int(c) for c in (op.get("responses") or {}) if c.isdigit() and c not in {"200", "201"})
        errs = ", ".join(str(c) for c in codes)
        lines.append(f"| {method} | `{path}` | {desc} | {perm} | {errs or '-'} |")
    lines.append("")

lines.append("## 附：请求/响应示例（关键新增接口）")
lines.append("")
lines.append("### POST /api/v1/auth/refresh-token（刷新令牌，一次性轮换）")
lines.append("```json")
lines.append("// 请求体")
lines.append('{"refresh_token": "xxxxxxxx"}')
lines.append("// 成功响应")
lines.append('{"code": 0, "message": "ok", "data": {"access_token": "新访问令牌(30min)", "refresh_token": "新刷新令牌(7天)"}}')
lines.append("// 失败响应(401)：令牌无效/过期/已拉黑")
lines.append('{"code": 401, "message": "刷新令牌无效或已过期", "data": null}')
lines.append("```")
lines.append("")
lines.append("### POST /api/v1/auth/logout（登出 + 刷新令牌拉黑）")
lines.append("```json")
lines.append("// 请求体（refresh_token 可选，推荐传入实现踢下线）")
lines.append('{"refresh_token": "xxxxxxxx"}')
lines.append("// 成功响应")
lines.append('{"code": 0, "message": "ok", "data": {}}')
lines.append("```")
lines.append("")
lines.append("### POST /api/v1/eval/runs/compare（A/B 两两对比）")
lines.append("```json")
lines.append("// 请求体：两个运行必须属于同一数据集")
lines.append('{"run_id_a": 1, "run_id_b": 2}')
lines.append("// 成功响应（节选）")
lines.append('{"code": 0, "message": "ok", "data": {')
lines.append('  "run_a": {...}, "run_b": {...},')
lines.append('  "summary": [{"metric": "faithfulness", "avg_a": 0.52, "avg_b": 0.68, "diff": 0.16}],')
lines.append('  "rows": [{"question": "q1", "a": {...}, "b": {...}, "diffs": {"faithfulness": 0.7}, "is_diff": true}],')
lines.append('  "diff_threshold": 0.1}}')
lines.append("// 失败响应(400)：非同一数据集")
lines.append('{"code": 400, "message": "两个评测运行必须属于同一数据集", "data": null}')
lines.append("```")
lines.append("")
lines.append("### GET /api/v1/eval/runs/{run_id}/badcases（BadCase 分页）")
lines.append("```")
lines.append("GET /api/v1/eval/runs/1/badcases?metric=faithfulness&threshold=0.6&page=1&page_size=20")
lines.append("// 成功响应（节选）：NULL 指标不参与筛选（统一空值口径），按指标升序（最差在前）")
lines.append('{"code": 0, "message": "ok", "data": {"total": 3, "page": 1, "page_size": 20, "items": [')
lines.append('  {"question": "...", "ground_truth": "...", "answer": "...", "retrieved_contexts": [...],')
lines.append('   "faithfulness": 0.32, "answer_relevancy": null, "context_precision": 0.55, "context_recall": 0.41}]}}')
lines.append("```")
lines.append("")
lines.append("### GET /api/v1/eval/runs/{run_id}/stratum_metrics（分层指标聚合）")
lines.append("```json")
lines.append("// 成功响应：stratum=null 表示未分层；AVG 忽略 NULL（与全站空值口径一致）")
lines.append('{"code": 0, "message": "ok", "data": {"run_id": 1, "total": 12, "items": [')
lines.append('  {"stratum": "难例", "sample_count": 5, "metrics": {"faithfulness": 0.61, "answer_relevancy": 0.72, "context_precision": 0.58, "context_recall": 0.66}},')
lines.append('  {"stratum": null, "sample_count": 7, "metrics": {...}}]}}')
lines.append("```")
lines.append("")
lines.append("## 附：响应体 Schema 总览")
lines.append("")
lines.append("完整 Pydantic 模型见 openapi.json `components.schemas`：")
lines.append("")
lines.append("| Schema | 用途 |")
lines.append("|---|---|")
for name in sorted(schemas):
    d = (schemas[name].get("description") or "").splitlines()
    desc = (d[0][:60] if d else "")
    lines.append(f"| `{name}` | {desc} |")
lines.append("")

(ROOT / "docs" / "接口文档.md").write_text("\n".join(lines), encoding="utf-8")
print("OK docs/接口文档.md", len(lines), "lines, groups:", list(groups))

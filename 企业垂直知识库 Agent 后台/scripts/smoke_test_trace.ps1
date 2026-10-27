# =====================================================================
# Trace 模块冒烟测试脚本
# 用法：先启动服务（uvicorn app.main:app --host 0.0.0.0 --port 8000），
#       再执行：  .\scripts\smoke_test_trace.ps1
# 可选参数：-AdminUser admin -AdminPass 你的管理员密码
# =====================================================================
param(
    [string]$BaseUrl   = "http://127.0.0.1:8000",
    [string]$AdminUser = "admin",
    [string]$AdminPass = "your_admin_password"
)

$ErrorActionPreference = "Stop"

function Send-JsonBody($Body) {
    # 用 UTF-8 字节发送，避免 PowerShell 5.1 把中文按本地编码误传
    return [System.Text.Encoding]::UTF8.GetBytes($Body)
}

Write-Host "=== 1. 管理员登录 ==="
$loginBody = @{ username = $AdminUser; password = $AdminPass } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/auth/login" `
    -ContentType "application/json; charset=utf-8" -Body (Send-JsonBody $loginBody)
$token = $login.data.access_token
$headers = @{ Authorization = "Bearer $token" }
Write-Host "登录成功，token 前 12 位：$($token.Substring(0, 12))..."

Write-Host "`n=== 2. 新建会话 ==="
$conv = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/conversations" `
    -Headers $headers -ContentType "application/json" -Body '{}'
$cid = $conv.data.id
Write-Host "会话ID：$cid"

Write-Host "`n=== 3. 发起 db_query 路由问答（触发 Trace）==="
$chatBody = @{ question = "查询所有工单" } | ConvertTo-Json
$chat = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/conversations/$cid/chat" `
    -Headers $headers -ContentType "application/json; charset=utf-8" -Body (Send-JsonBody $chatBody)
$traceId = $chat.data.trace_id
Write-Host "agent_route：$($chat.data.agent_route)"
Write-Host "trace_id：$traceId"

Write-Host "`n=== 4. 查询 Trace 列表（管理员）==="
$list = Invoke-RestMethod -Method Get "$BaseUrl/api/v1/traces?page=1&size=10" -Headers $headers
Write-Host "Trace 总数：$($list.data.total)"

Write-Host "`n=== 5. Trace 详情（含工具调用）==="
$detail = Invoke-RestMethod -Method Get "$BaseUrl/api/v1/traces/$traceId" -Headers $headers
Write-Host "route=$($detail.data.agent_route) status=$($detail.data.status) tokens=$($detail.data.total_tokens) latency=$($detail.data.total_latency_ms)ms"
Write-Host "tool_calls 数量：$($detail.data.tool_calls.Count)"

Write-Host "`n=== 6. 工具调用明细 ==="
$calls = Invoke-RestMethod -Method Get "$BaseUrl/api/v1/traces/$traceId/tool-calls" -Headers $headers
Write-Host "工具调用总数：$($calls.data.total)"
foreach ($c in $calls.data.items) {
    Write-Host ("  - {0} / {1} / {2}ms / {3}" -f $c.step_name, $c.tool_name, $c.latency_ms, $c.status)
}

Write-Host "`n=== 7. 汇总 ==="
$summary = Invoke-RestMethod -Method Get "$BaseUrl/api/v1/traces/stats/summary" -Headers $headers
Write-Host ("total={0} avg_latency={1}ms token_sum={2} llm_calls={3}" -f `
    $summary.data.total, $summary.data.avg_latency_ms, $summary.data.token_sum, $summary.data.llm_call_sum)
Write-Host ("路由分布：{0}" -f ($summary.data.route_distribution | ConvertTo-Json -Compress))

Write-Host "`n=== 冒烟测试通过 ==="

Write-Host "`n提示：验证普通用户越权访问请执行（期望返回 403）："
Write-Host "  Invoke-RestMethod -Method Get '$BaseUrl/api/v1/traces' -Headers @{Authorization='Bearer <普通用户token>'}"

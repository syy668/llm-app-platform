# =====================================================================
# SSE 流式问答接口冒烟测试脚本
# 用法：先启动服务（uvicorn app.main:app --host 0.0.0.0 --port 8000），
#       再执行：  .\scripts\smoke_test_stream.ps1 -AdminPass "你的管理员密码"
# 说明：用 curl.exe -N 读取 text/event-stream 流；会真实调用 DeepSeek。
# =====================================================================
param(
    [string]$BaseUrl   = "http://127.0.0.1:8000",
    [string]$AdminUser = "admin",
    [string]$AdminPass = "your_admin_password"
)

$ErrorActionPreference = "Stop"

function Write-Utf8JsonFile($path, $obj) {
    # 写 UTF-8 无 BOM 的 JSON 请求体，避免中文乱码
    $json = $obj | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host "=== 1. 管理员登录 ==="
$loginBody = @{ username = $AdminUser; password = $AdminPass } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/auth/login" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($loginBody))
$token = $login.data.access_token
$authHeader = "Authorization: Bearer $token"
Write-Host "登录成功"

Write-Host "`n=== 2. 新建会话 ==="
$conv = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/conversations" `
    -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body '{}'
$cid = $conv.data.id
Write-Host "会话ID: $cid"

$bodyFile = Join-Path $env:TEMP "stream_body.json"

Write-Host "`n=== 3. 流式问答（db_query 路由：逐 token delta）==="
Write-Utf8JsonFile $bodyFile @{ question = "查询所有工单" }
Write-Host "--- SSE 原始流（event: route -> delta* -> done）---"
curl.exe -N -s -X POST "$BaseUrl/api/v1/conversations/$cid/chat/stream" `
    -H "$authHeader" -H "Content-Type: application/json" --data-binary "@$bodyFile"
Write-Host "`n--- 流结束 ---"

Write-Host "`n=== 4. 流式问答（report 路由：直接输出 Markdown）==="
Write-Utf8JsonFile $bodyFile @{ question = "生成本月工单汇总报表" }
Write-Host "--- SSE 原始流（event: route -> report -> done）---"
curl.exe -N -s -X POST "$BaseUrl/api/v1/conversations/$cid/chat/stream" `
    -H "$authHeader" -H "Content-Type: application/json" --data-binary "@$bodyFile"
Write-Host "`n--- 流结束 ---"

Write-Host "`n=== 冒烟测试完成 ==="
Write-Host "验证点："
Write-Host "  - 步骤3 应看到 event: route(route=db_query) + 大量 event: delta + event: done"
Write-Host "  - 步骤4 应看到 event: route(route=report) + event: report(含 Markdown) + event: done"
Write-Host "  - 验证 trace 已落库：GET /api/v1/traces?page=1&size=5（管理员）"
Write-Host "  - 验证消息已落库：GET /api/v1/conversations/$cid/messages"

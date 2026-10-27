# =====================================================================
# 迭代补齐功能冒烟测试：接口限流 + 评测 CSV 导出（工具重试见 pytest 单测）
# 用法：先启动服务，再执行：  .\scripts\smoke_patch.ps1 -AdminPass "密码"
# 限流说明：需 .env 中 FAKE_REDIS=false 且配置 AI_RATE_LIMIT_PER_MINUTE（默认 30）。
# =====================================================================
param(
    [string]$BaseUrl   = "http://127.0.0.1:8000",
    [string]$AdminUser = "admin",
    [string]$AdminPass = "your_admin_password"
)
$ErrorActionPreference = "Stop"

function Write-Utf8JsonFile($path, $obj) {
    [System.IO.File]::WriteAllText($path, ($obj | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host "=== 1. 管理员登录 ==="
$login = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/auth/login" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes((@{username=$AdminUser; password=$AdminPass} | ConvertTo-Json)))
$token = $login.data.access_token
$h = @{ Authorization = "Bearer $token" }
$authHeader = "Authorization: Bearer $token"
Write-Host "登录成功"

Write-Host "`n=== 2. 评测 CSV 导出 ==="
$runs = Invoke-RestMethod -Method Get "$BaseUrl/api/v1/eval/runs?page=1&size=10" -Headers $h
if ($runs.data.total -eq 0) {
    Write-Host "无评测运行记录，跳过 CSV 导出（可先跑 smoke_test_eval.ps1 生成一条）"
} else {
    $rid = $runs.data.items[0].id
    Write-Host "导出 run_id=$rid 的 CSV ..."
    $out = Join-Path $env:TEMP "eval_run_$rid.csv"
    curl.exe -s "$BaseUrl/api/v1/eval/runs/$rid/export" -H "$authHeader" -o $out
    Write-Host "CSV 已保存到 $out，前几行："
    Get-Content $out -TotalCount 4
}

Write-Host "`n=== 3. 接口限流（连续请求 chat，观察是否 429）==="
$conv = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/conversations" -Headers $h -ContentType "application/json" -Body '{}'
$cid = $conv.data.id
$bodyFile = Join-Path $env:TEMP "patch_body.json"
Write-Utf8JsonFile $bodyFile @{ question = "你好" }
Write-Host "会话ID: $cid，连续发 5 次 chat："
for ($i = 1; $i -le 5; $i++) {
    $code = curl.exe -s -o NUL -w "%{http_code}" -X POST "$BaseUrl/api/v1/conversations/$cid/chat" `
        -H "$authHeader" -H "Content-Type: application/json" --data-binary "@$bodyFile"
    Write-Host ("  第 {0} 次 HTTP 状态码: {1}" -f $i, $code)
}
Write-Host "`n若 FAKE_REDIS=false 且 AI_RATE_LIMIT_PER_MINUTE 较低，后续请求应返回 429。"
Write-Host "（FAKE_REDIS=true 时按约束直接跳过限流，全部 200）"

Write-Host "`n=== 冒烟测试完成 ==="
Write-Host "工具重试验证（pytest）：.\.venv\Scripts\python -m pytest tests\test_retry.py -q"

# =====================================================================
# RAGAS 评测模块冒烟测试脚本
# 用法：先启动服务（uvicorn app.main:app --host 0.0.0.0 --port 8000），
#       再执行：  .\scripts\smoke_test_eval.ps1 -AdminPass "你的管理员密码"
# 说明：评测样本用 db_query 路由（无需知识库文档），会真实调用 DeepSeek 算指标。
# =====================================================================
param(
    [string]$BaseUrl   = "http://127.0.0.1:8000",
    [string]$AdminUser = "admin",
    [string]$AdminPass = "your_admin_password"
)

$ErrorActionPreference = "Stop"

function ConvertTo-Utf8Body($obj) {
    # 用 UTF-8 字节发送，避免 PowerShell 5.1 中文乱码
    return [System.Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Depth 10))
}

Write-Host "=== 1. 管理员登录 ==="
$login = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/auth/login" `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Utf8Body @{ username = $AdminUser; password = $AdminPass })
$h = @{ Authorization = "Bearer $($login.data.access_token)" }
Write-Host "登录成功"

Write-Host "`n=== 2. 创建知识库（评测数据集需要绑定 kb_id）==="
$kb = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/kbs" -Headers $h `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Utf8Body @{ name = "评测用知识库"; description = "冒烟测试" })
$kbId = $kb.data.id
Write-Host "kb_id: $kbId"

Write-Host "`n=== 3. 创建评测数据集 ==="
$ds = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/eval/datasets" -Headers $h `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Utf8Body @{ kb_id = $kbId; name = "冒烟测试数据集"; description = "db_query 样本" })
$dsId = $ds.data.id
Write-Host "dataset_id: $dsId"

Write-Host "`n=== 4. 批量导入评测样本 ==="
$items = @(
    @{ question = "查询所有工单"; reference_answer = "工单列表应包含 TK2026001 数据库服务器CPU持续告警等记录。"; reference_contexts = @("工单表包含多条工单记录") },
    @{ question = "查询所有告警"; reference_answer = "告警列表应包含 CPU使用率过高等告警记录。"; reference_contexts = @("告警表包含多条告警记录") }
)
$imp = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/eval/datasets/$dsId/items" -Headers $h `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Utf8Body @{ items = $items })
Write-Host "导入条数: $($imp.data.imported)"

Write-Host "`n=== 5. 触发异步评测任务 ==="
$run = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/eval/runs" -Headers $h `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Utf8Body @{ dataset_id = $dsId })
$runId = $run.data.id
Write-Host "run_id: $runId  初始状态: $($run.data.status)"

Write-Host "`n=== 6. 轮询任务状态（pending/running -> done/failed）==="
$r = $null
do {
    Start-Sleep -Seconds 5
    $r = Invoke-RestMethod -Method Get "$BaseUrl/api/v1/eval/runs/$runId" -Headers $h
    Write-Host ("  status={0}  success={1}/{2}" -f $r.data.status, $r.data.success_items, $r.data.total_items)
} while ($r.data.status -in @('pending', 'running'))

Write-Host "`n=== 7. 运行详情（汇总 metrics）==="
Write-Host ("  metrics: " + ($r.data.metrics | ConvertTo-Json -Compress))

Write-Host "`n=== 8. 单条得分详情 ==="
$scores = Invoke-RestMethod -Method Get "$BaseUrl/api/v1/eval/runs/$runId/scores?page=1&size=10" -Headers $h
foreach ($s in $scores.data.items) {
    Write-Host ("  question={0} faithfulness={1} answer_relevancy={2} context_precision={3} context_recall={4}" -f `
        $s.question, $s.faithfulness, $s.answer_relevancy, $s.context_precision, $s.context_recall)
}

Write-Host "`n=== 冒烟测试完成 ==="
Write-Host "提示：验证普通用户越权（期望 403）："
Write-Host "  Invoke-RestMethod -Method Get '$BaseUrl/api/v1/eval/runs' -Headers @{Authorization='Bearer <普通用户token>'}"

# =====================================================================
# 企业垂直知识库 Agent 后台 —— 全链路功能验收冒烟测试（合并版）
#
# 覆盖范围：
#   1. 登录鉴权（登录 / 当前用户 / 刷新令牌 / 登出）
#   2. 知识库 CRUD（创建 / 详情 / 更新 / 统计 / 上传文档 / RAG 问答 / 删除）
#   3. 会话管理（创建 / 列表 / 消息 / 删除）
#   4. 四路普通问答（rag / db_query / report / direct）
#   5. SSE 流式问答（route / delta / report / done / error 事件）
#   6. Trace 日志查询（列表 / 详情 / 工具调用 / 汇总）
#   7. RAGAS 评测全流程（数据集 / 导入样本 / 运行 / 轮询 / 得分）
#   8. RBAC 越权校验（401 未登录 / 403 跨角色 / 403 跨会话 / 403 跨知识库）
#
# 两套账号：管理员(admin，外部传入密码) + 普通用户(脚本自动创建)
#
# 用法：
#   1) 先启动后端：uvicorn app.main:app --host 0.0.0.0 --port 8000
#   2) 执行：
#        .\scripts\all_smoke_test.ps1 -AdminPass "管理员密码"
#   可选参数：
#        -BaseUrl http://127.0.0.1:8000
#        -AdminUser admin
#        -NormalUser smoke_user   -NormalPass "SmokeTest@2026"
#        -SkipRagas   跳过 RAGAS 评测段（评测需真实 DeepSeek 且耗时较长）
#
# 前置：后端 .env 已配置 MySQL / Redis / DeepSeek(LLM_API_KEY)，
#       否则四路问答 / RAGAS 评测段会因缺少 LLM 而失败。
# =====================================================================
[CmdletBinding()]
param(
    [string]$BaseUrl     = "http://127.0.0.1:8000",
    [string]$AdminUser   = "admin",
    [Parameter(Mandatory = $true)][string]$AdminPass,
    [string]$NormalUser  = "smoke_user",
    [string]$NormalPass  = "SmokeTest@2026",
    [switch]$SkipRagas
)

$ErrorActionPreference = "Stop"

# 加载 System.Net.Http（文件上传 / SSE 使用 HttpClient，PS 5.1 需显式加载程序集）
Add-Type -AssemblyName System.Net.Http

# ---------------- 中文编码处理（PowerShell 5.1 兼容） ----------------
# 统一 UTF-8：发送中文请求体、接收中文响应、向原生命令(cmd/curl)传中文均按 UTF-8 处理
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try { chcp 65001 | Out-Null } catch { }

$script:PassCount = 0
$script:FailCount = 0
$script:WarnCount = 0
$script:ObservedRoutes = @()
$script:DbQueryTraceId = $null

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host ("=" * 72)
    Write-Host ("  " + $Title)
    Write-Host ("=" * 72)
}

function ConvertTo-Utf8Bytes($obj) {
    $json = $obj | ConvertTo-Json -Depth 10
    return [System.Text.Encoding]::UTF8.GetBytes($json)
}

# 统一 HTTP 请求封装：成功返回解析后的 {code,message,data}；
# 失败捕获 4xx/5xx 并读取响应体（PS 5.1 兼容，不抛异常）。
function Invoke-Api {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Url,
        [hashtable]$Headers = @{},
        [byte[]]$Body = $null
    )
    $params = @{ Method = $Method; Uri = $Url; Headers = $Headers; ErrorAction = "Stop" }
    if ($null -ne $Body) {
        $params["Body"] = $Body
        $params["ContentType"] = "application/json; charset=utf-8"
    }
    try {
        $resp = Invoke-RestMethod @params
        return [pscustomobject]@{ Ok = $true;  Status = 200; Code = $resp.code; Message = $resp.message; Data = $resp.data }
    }
    catch {
        $status = 0; $code = -1; $message = $_.Exception.Message; $data = $null
        if ($_.Exception.Response) {
            $webResp = $_.Exception.Response
            $status = [int]$webResp.StatusCode
            try {
                $reader = New-Object System.IO.StreamReader($webResp.GetResponseStream(), [System.Text.Encoding]::UTF8)
                $parsed = ($reader.ReadToEnd()) | ConvertFrom-Json
                $code = $parsed.code; $message = $parsed.message; $data = $parsed.data
            } catch { }
        }
        return [pscustomobject]@{ Ok = $false; Status = $status; Code = $code; Message = $message; Data = $data }
    }
}

# 文件上传封装（multipart/form-data）：用 .NET HttpClient，天然 UTF-8，
# 避免 curl.exe 在中文路径 / 中文文件名下出错。
function Invoke-UploadText {
    param([string]$Url, [string]$Token, [string]$FileName, [string]$Text)
    $client = New-Object System.Net.Http.HttpClient
    $content = $null
    try {
        $client.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", $Token)
        $content = New-Object System.Net.Http.MultipartFormDataContent
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $fileContent = New-Object System.Net.Http.ByteArrayContent -ArgumentList (,$bytes)
        [void]$content.Add($fileContent, "file", $FileName)
        $resp = $client.PostAsync($Url, $content).Result
        $body = $resp.Content.ReadAsStringAsync().Result
        return ($body | ConvertFrom-Json)
    }
    finally {
        if ($null -ne $content) { $content.Dispose() }
        $client.Dispose()
    }
}

# SSE 流式请求：使用 HttpClient 读取 text/event-stream（避免 curl.exe 在 PS 5.1 下
# 标准输入 / 流式输出捕获不稳定），返回完整事件文本，调用方用 -match 校验事件。
function Invoke-Sse {
    param([string]$Url, [string]$Token, [hashtable]$Body)
    $json = $Body | ConvertTo-Json -Depth 5
    $client = New-Object System.Net.Http.HttpClient
    $content = $null
    try {
        $client.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", $Token)
        $content = New-Object System.Net.Http.StringContent($json, [System.Text.Encoding]::UTF8, "application/json")
        $request = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Post, $Url)
        $request.Content = $content
        $resp = $client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).Result
        $stream = $resp.Content.ReadAsStreamAsync().Result
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
    }
    finally {
        if ($null -ne $content) { $content.Dispose() }
        $client.Dispose()
    }
}

# ---------------- 断言与计数 ----------------
function Write-Pass([string]$Name) {
    $script:PassCount = $script:PassCount + 1
    Write-Host ("  [PASS] " + $Name) -ForegroundColor Green
}
function Write-Fail([string]$Name, [string]$Detail = "") {
    $script:FailCount = $script:FailCount + 1
    if ($Detail) { Write-Host ("  [FAIL] " + $Name + "  " + $Detail) -ForegroundColor Red }
    else { Write-Host ("  [FAIL] " + $Name) -ForegroundColor Red }
}
function Write-WarnMsg([string]$Msg) {
    $script:WarnCount = $script:WarnCount + 1
    Write-Host ("  [WARN] " + $Msg) -ForegroundColor Yellow
}

function Assert-Code($Resp, [int]$ExpectCode, [string]$Name) {
    if ($Resp.Ok -and $Resp.Code -eq $ExpectCode) { Write-Pass ($Name + "  (code=0)") }
    else { Write-Fail $Name ("期望 code=$ExpectCode，实际 status=$($Resp.Status) code=$($Resp.Code) msg=$($Resp.Message)") }
}
function Assert-Http($Resp, [int]$ExpectStatus, [string]$Name) {
    if ($Resp.Status -eq $ExpectStatus) { Write-Pass ($Name + "  (HTTP $ExpectStatus)") }
    else { Write-Fail $Name ("期望 HTTP $ExpectStatus，实际 status=$($Resp.Status) code=$($Resp.Code) msg=$($Resp.Message)") }
}
function Assert-True([bool]$Cond, [string]$Name, [string]$Detail = "") {
    if ($Cond) { Write-Pass $Name } else { Write-Fail $Name $Detail }
}

# =====================================================================
# 0. 前置：健康检查
# =====================================================================
Write-Section "0. 服务健康检查"
$health = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/health"
if (-not $health.Ok -or $health.Code -ne 0) {
    Write-Host "  [错误] 无法连接后端服务，请先启动：uvicorn app.main:app --host 0.0.0.0 --port 8000" -ForegroundColor Red
    exit 1
}
Write-Host ("  服务状态={0}  db={1}  redis={2}" -f $health.Data.status, $health.Data.db, $health.Data.redis) -ForegroundColor Cyan

# =====================================================================
# 1. 登录鉴权（管理员）
# =====================================================================
Write-Section "1. 登录鉴权（管理员）"
$login = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/auth/login" -Body (ConvertTo-Utf8Bytes @{ username = $AdminUser; password = $AdminPass })
Assert-Code $login 0 "管理员登录"
if (-not $login.Ok -or $login.Code -ne 0) {
    Write-Host "  无法继续：管理员登录失败，请检查 -AdminPass。" -ForegroundColor Red
    exit 1
}
$adminToken = $login.Data.access_token
$adminRefresh = $login.Data.refresh_token
$adminHeaders = @{ Authorization = "Bearer $adminToken" }

$me = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/auth/me" -Headers $adminHeaders
Assert-Code $me 0 "获取当前用户 /auth/me"
Assert-True ($me.Data.role -eq "admin") "当前用户角色为 admin"

$refresh = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/auth/refresh" -Body (ConvertTo-Utf8Bytes @{ refresh_token = $adminRefresh })
Assert-Code $refresh 0 "刷新令牌 /auth/refresh 换发新 access_token"

# =====================================================================
# 2. 用户管理 + 创建普通用户（两套账号）
# =====================================================================
Write-Section "2. 创建普通用户（管理员操作）"
$createUser = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/users" -Headers $adminHeaders `
    -Body (ConvertTo-Utf8Bytes @{ username = $NormalUser; password = $NormalPass; display_name = "冒烟普通用户"; role = "user" })
if ($createUser.Ok -and $createUser.Code -eq 0) {
    Write-Pass "创建普通用户 $NormalUser"
}
elseif ($createUser.Status -eq 409) {
    Write-WarnMsg "普通用户 $NormalUser 已存在，改用管理员重置密码保证可用"
    $ul = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/users?page=1&size=100&keyword=$NormalUser" -Headers $adminHeaders
    $uid = ($ul.Data.items | Where-Object { $_.username -eq $NormalUser } | Select-Object -First 1).id
    if ($uid) {
        $rp = Invoke-Api -Method Put -Url "$BaseUrl/api/v1/users/$uid/reset-password" -Headers $adminHeaders `
            -Body (ConvertTo-Utf8Bytes @{ new_password = $NormalPass })
        Assert-Code $rp 0 "重置普通用户密码"
    } else {
        Write-Fail "重置普通用户密码" "用户列表未找到 $NormalUser"
    }
}
else {
    Write-Fail "创建普通用户" "status=$($createUser.Status) msg=$($createUser.Message)"
}

$userList = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/users?page=1&size=20" -Headers $adminHeaders
Assert-Code $userList 0 "用户列表（管理员）"

# =====================================================================
# 3. 普通用户登录 + RBAC 越权校验
# =====================================================================
Write-Section "3. 普通用户登录 + RBAC 越权校验"
$login2 = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/auth/login" -Body (ConvertTo-Utf8Bytes @{ username = $NormalUser; password = $NormalPass })
Assert-Code $login2 0 "普通用户登录"
$userToken = $login2.Data.access_token
$userHeaders = @{ Authorization = "Bearer $userToken" }

$noAuth = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/auth/me"
Assert-Http $noAuth 401 "未登录访问 /auth/me -> 401"

$badToken = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/auth/me" -Headers @{ Authorization = "Bearer invalid.token.xxx" }
Assert-Http $badToken 401 "伪造 token -> 401"

$forbidUsers = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/users" -Headers $userHeaders
Assert-Http $forbidUsers 403 "普通用户访问 /users -> 403"

$forbidTraces = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/traces" -Headers $userHeaders
Assert-Http $forbidTraces 403 "普通用户访问 /traces -> 403"

$forbidEval = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/eval/datasets" -Headers $userHeaders
Assert-Http $forbidEval 403 "普通用户访问 /eval/datasets -> 403"

# =====================================================================
# 4. 知识库 CRUD
# =====================================================================
Write-Section "4. 知识库 CRUD（管理员）"
$kb = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/kbs" -Headers $adminHeaders `
    -Body (ConvertTo-Utf8Bytes @{ name = "冒烟测试知识库"; description = "全链路冒烟测试" })
Assert-Code $kb 0 "创建知识库"
$kbId = $kb.Data.id

$kbGet = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/kbs/$kbId" -Headers $adminHeaders
Assert-Code $kbGet 0 "知识库详情"

$kbUpd = Invoke-Api -Method Put -Url "$BaseUrl/api/v1/kbs/$kbId" -Headers $adminHeaders `
    -Body (ConvertTo-Utf8Bytes @{ description = "更新后的描述" })
Assert-Code $kbUpd 0 "更新知识库"

# 上传文档（供 rag 路由检索）
$docText = "企业垂直知识库 Agent 测试文档。`n数据库服务器的 CPU 使用率超过 90%，需要重点关注。`n核心交换机位于机房 A，当前状态在线。"
$upload = Invoke-UploadText -Url "$BaseUrl/api/v1/kbs/$kbId/documents" -Token $adminToken -FileName "kb_smoke_doc.txt" -Text $docText
Assert-True ($upload.code -eq 0) "上传文档" ("上传返回 code=$($upload.code)")

$docList = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/kbs/$kbId/documents?page=1&size=10" -Headers $adminHeaders
Assert-Code $docList 0 "文档列表"

$kbQuery = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/kbs/$kbId/query" -Headers $adminHeaders `
    -Body (ConvertTo-Utf8Bytes @{ question = "数据库服务器的 CPU 使用率是多少" })
Assert-Code $kbQuery 0 "RAG 直接问答 /kbs/{id}/query"

$stats = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/kbs/$kbId/stats" -Headers $adminHeaders
Assert-Code $stats 0 "知识库统计"
Write-Host ("        统计：doc=$($stats.Data.doc_count) chunk=$($stats.Data.chunk_count) token=$($stats.Data.token_sum)")

# 跨知识库越权：普通用户访问管理员知识库 -> 403
$crossKb = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/kbs/$kbId" -Headers $userHeaders
Assert-Http $crossKb 403 "普通用户访问他人知识库 -> 403"

# =====================================================================
# 5. 会话管理
# =====================================================================
Write-Section "5. 会话管理（管理员）"
$conv = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/conversations" -Headers $adminHeaders `
    -Body (ConvertTo-Utf8Bytes @{ kb_id = $kbId })
Assert-Code $conv 0 "创建会话"
$cid = $conv.Data.id

$convList = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/conversations?page=1&size=20" -Headers $adminHeaders
Assert-Code $convList 0 "会话列表"

# 跨会话越权：普通用户访问管理员会话 -> 403
$crossConv = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/conversations/$cid/messages" -Headers $userHeaders
Assert-Http $crossConv 403 "普通用户访问他人会话 -> 403"

# =====================================================================
# 6. 四路普通问答（rag / db_query / report / direct）
# =====================================================================
Write-Section "6. 四路普通问答"
$routes = @(
    @{ Name = "rag";      Question = "请根据知识库文档回答：数据库服务器的 CPU 使用率是多少？"; Expect = "rag" },
    @{ Name = "db_query"; Question = "查询所有工单";                                         Expect = "db_query" },
    @{ Name = "report";   Question = "生成本月工单汇总报表";                                  Expect = "report" },
    @{ Name = "direct";   Question = "你好，请介绍一下你自己";                                Expect = "direct" }
)
foreach ($r in $routes) {
    $chat = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/conversations/$cid/chat" -Headers $adminHeaders `
        -Body (ConvertTo-Utf8Bytes @{ question = $r.Question })
    Assert-Code $chat 0 ("四路问答 [" + $r.Name + "] 返回 code=0")
    $answer = [string]$chat.Data.answer
    Assert-True (-not [string]::IsNullOrWhiteSpace($answer)) ("四路问答 [" + $r.Name + "] answer 非空")
    $actual = [string]$chat.Data.agent_route
    $script:ObservedRoutes += $actual
    Write-Host ("        实际路由={0}（期望={1}）" -f $actual, $r.Expect) -ForegroundColor Cyan
    if ($actual -ne $r.Expect) { Write-WarnMsg ("[" + $r.Name + "] 路由与期望不一致（LLM 分类存在波动，可重跑）") }

    if ($r.Name -eq "report") {
        # report 路由不走 LLM 合成，输出内容应包含报表工具生成的固定 Markdown 尾注
        Assert-True ($answer -match "报表由系统自动生成") "report 路由直出 Markdown（未走 LLM）"
    }
    if ($r.Name -eq "db_query" -and $null -eq $script:DbQueryTraceId) {
        $script:DbQueryTraceId = $chat.Data.trace_id
    }
}

# 四路覆盖率统计
foreach ($want in @("rag", "db_query", "report", "direct")) {
    if ($script:ObservedRoutes -contains $want) { Write-Pass ("四路覆盖：$want 已命中") }
    else { Write-WarnMsg ("四路覆盖：$want 未命中，建议重跑或更换提问措辞") }
}

# =====================================================================
# 7. SSE 流式问答
# =====================================================================
Write-Section "7. SSE 流式问答"
$sse1 = Invoke-Sse -Url "$BaseUrl/api/v1/conversations/$cid/chat/stream" -Token $adminToken -Body @{ question = "查询所有工单" }
Assert-True ($sse1 -match "event: route") "SSE 含 route 事件"
Assert-True ($sse1 -match "event: done") "SSE 含 done 事件"
if ($sse1 -match "event: delta") { Write-Pass "SSE 含 delta 事件（流式片段）" }
else { Write-WarnMsg "SSE 未观测到 delta 事件（可能首包即完成或走了 report）" }

$sse2 = Invoke-Sse -Url "$BaseUrl/api/v1/conversations/$cid/chat/stream" -Token $adminToken -Body @{ question = "生成本月工单汇总报表" }
Assert-True ($sse2 -match "event: report") "SSE report 路由输出 report 事件"
Assert-True ($sse2 -match "报表由系统自动生成") "SSE report 事件内容为 Markdown（未走 LLM）"

$msgList = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/conversations/$cid/messages?page=1&size=50" -Headers $adminHeaders
Assert-Code $msgList 0 "会话消息列表（含流式/普通问答落库）"

# =====================================================================
# 8. Trace 日志查询
# =====================================================================
Write-Section "8. Trace 日志查询（管理员）"
$traces = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/traces?page=1&size=20" -Headers $adminHeaders
Assert-Code $traces 0 "Trace 列表"
Assert-True ($traces.Data.total -gt 0) "Trace 列表有数据"

$traceId = $script:DbQueryTraceId
if ($null -ne $traceId) {
    $detail = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/traces/$traceId" -Headers $adminHeaders
    Assert-Code $detail 0 "Trace 详情"
    Write-Host ("        route={0} status={1} tokens={2} latency={3}ms tool_calls={4}" -f `
        $detail.Data.agent_route, $detail.Data.status, $detail.Data.total_tokens, $detail.Data.total_latency_ms, $detail.Data.tool_calls.Count)

    $calls = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/traces/$traceId/tool-calls" -Headers $adminHeaders
    Assert-Code $calls 0 "Trace 工具调用明细"
} else {
    Write-WarnMsg "未采集到 db_query 的 trace_id，跳过 Trace 详情/工具调用"
}

$summary = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/traces/stats/summary" -Headers $adminHeaders
Assert-Code $summary 0 "Trace 汇总"
Write-Host ("        汇总：total={0} avg_latency={1}ms token_sum={2}" -f `
    $summary.Data.total, $summary.Data.avg_latency_ms, $summary.Data.token_sum)

# =====================================================================
# 9. RAGAS 评测全流程
# =====================================================================
if ($SkipRagas) {
    Write-Section "9. RAGAS 评测（已跳过）"
}
else {
    Write-Section "9. RAGAS 评测全流程"
    $ds = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/eval/datasets" -Headers $adminHeaders `
        -Body (ConvertTo-Utf8Bytes @{ kb_id = $kbId; name = "冒烟评测数据集"; description = "db_query 样本" })
    Assert-Code $ds 0 "创建评测数据集"
    $dsId = $ds.Data.id

    $items = @(
        @{ question = "查询所有工单"; reference_answer = "工单列表应包含多条工单记录。"; reference_contexts = @("工单表包含多条工单记录") },
        @{ question = "查询所有告警"; reference_answer = "告警列表应包含多条告警记录。"; reference_contexts = @("告警表包含多条告警记录") }
    )
    $imp = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/eval/datasets/$dsId/items" -Headers $adminHeaders `
        -Body (ConvertTo-Utf8Bytes @{ items = $items })
    Assert-Code $imp 0 "批量导入评测样本"
    Write-Host ("        导入条数：{0}" -f $imp.Data.imported)

    $run = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/eval/runs" -Headers $adminHeaders `
        -Body (ConvertTo-Utf8Bytes @{ dataset_id = $dsId })
    Assert-Code $run 0 "创建评测运行（异步）"
    $runId = $run.Data.id

    $r = $null
    $deadline = (Get-Date).AddSeconds(180)
    do {
        Start-Sleep -Seconds 4
        $r = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/eval/runs/$runId" -Headers $adminHeaders
        Write-Host ("        轮询：status={0} success={1}/{2}" -f $r.Data.status, $r.Data.success_items, $r.Data.total_items)
    } while (($r.Data.status -in @('pending', 'running')) -and ((Get-Date) -lt $deadline))

    Assert-True ($r.Data.status -eq "done") "评测任务完成(done)" ("实际 status=$($r.Data.status) error=$($r.Data.error_msg)")
    if ($null -ne $r.Data.metrics) {
        Write-Host ("        汇总指标：" + ($r.Data.metrics | ConvertTo-Json -Compress))
    } else {
        Write-WarnMsg "metrics 为空（可能 LLM 不可用导致样本全部失败，请检查 LLM_API_KEY）"
    }

    $scores = Invoke-Api -Method Get -Url "$BaseUrl/api/v1/eval/runs/$runId/scores?page=1&size=10" -Headers $adminHeaders
    Assert-Code $scores 0 "评测得分列表"
}

# =====================================================================
# 10. 清理
# =====================================================================
Write-Section "10. 清理测试数据"
$delConv = Invoke-Api -Method Delete -Url "$BaseUrl/api/v1/conversations/$cid" -Headers $adminHeaders
Assert-Code $delConv 0 "删除会话"

$delKb = Invoke-Api -Method Delete -Url "$BaseUrl/api/v1/kbs/$kbId" -Headers $adminHeaders
Assert-Code $delKb 0 "删除知识库（级联清理文档/向量）"

$logout = Invoke-Api -Method Post -Url "$BaseUrl/api/v1/auth/logout" -Headers $adminHeaders -Body (ConvertTo-Utf8Bytes @{})
Assert-Code $logout 0 "管理员登出"

# =====================================================================
# 汇总
# =====================================================================
Write-Section "测试汇总"
Write-Host ("  通过: {0}   失败: {1}   警告: {2}" -f $script:PassCount, $script:FailCount, $script:WarnCount)
if ($script:FailCount -gt 0) {
    Write-Host "  结论：存在失败项，请根据上方 [FAIL] 逐项排查。" -ForegroundColor Red
    exit 1
}
else {
    Write-Host "  结论：功能验收全部通过（[WARN] 项需人工确认，不影响主流程）。" -ForegroundColor Green
    exit 0
}

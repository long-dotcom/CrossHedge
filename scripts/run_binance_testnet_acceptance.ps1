param(
    [Parameter(Mandatory = $true)]
    [string]$Token,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Symbol = "GOLD",
    [string]$Confirmation = "",
    [int]$TimeoutSeconds = 180,
    [string]$OutputPath = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ExpectedConfirmation = "RUN BINANCE TESTNET ACCEPTANCE"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $OutputPath) {
    $OutputPath = Join-Path $Root ".run\acceptance\binance-$Timestamp.json"
}

function Invoke-CrossHedgeApi {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [object]$Body = $null,
        [string]$IdempotencyKey = ""
    )
    $headers = @{ Authorization = "Bearer $Token" }
    if ($IdempotencyKey) {
        $headers["Idempotency-Key"] = $IdempotencyKey
    }
    $parameters = @{
        Method = $Method
        Uri = "$($BaseUrl.TrimEnd('/'))$Path"
        Headers = $headers
        TimeoutSec = 15
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = $Body | ConvertTo-Json -Depth 10 -Compress
    }
    return Invoke-RestMethod @parameters
}

function Assert-ProbeEvidence {
    param(
        [Parameter(Mandatory = $true)][object]$Run,
        [Parameter(Mandatory = $true)][string]$EntrySide
    )
    $expectedPosition = if ($EntrySide -eq "buy") { "LONG" } else { "SHORT" }
    $expectedEntryOrder = $EntrySide.ToUpperInvariant()
    $expectedExitOrder = if ($EntrySide -eq "buy") { "SELL" } else { "BUY" }
    if ($Run.status -ne "FLAT") { throw "Probe 未回到 FLAT: status=$($Run.status), error=$($Run.error_message)" }
    if ([math]::Abs([double]$Run.residual_quantity) -gt 1e-12) { throw "Probe 残量不为零: $($Run.residual_quantity)" }
    if ([math]::Abs([double]$Run.final_position_quantity - [double]$Run.baseline_position_quantity) -gt 1e-12) {
        throw "Probe 最终仓位未恢复基线: baseline=$($Run.baseline_position_quantity), final=$($Run.final_position_quantity)"
    }
    if ($Run.entry.status -ne "COMPLETED" -or $Run.entry.order.status -ne "FILLED") { throw "Probe 入口未确认成交" }
    if ($Run.exit.status -ne "COMPLETED" -or $Run.exit.order.status -ne "FILLED") { throw "Probe 退出未确认成交" }
    if ($Run.entry.order_side -ne $expectedEntryOrder -or $Run.entry.position_side -ne $expectedPosition) {
        throw "Probe 入口方向或 PositionId 侧错误"
    }
    if ($Run.exit.order_side -ne $expectedExitOrder -or $Run.exit.position_side -ne $expectedPosition) {
        throw "Probe 退出方向或 PositionId 侧错误"
    }
    if ([bool]$Run.entry.venue_reduce_only -or [bool]$Run.exit.venue_reduce_only) {
        throw "Binance Hedge Mode 验收订单不得发送 reduce_only"
    }
}

function Wait-ProbeFlat {
    param([Parameter(Mandatory = $true)][int]$ProbeRunId)
    $deadline = (Get-Date).AddSeconds([math]::Max($TimeoutSeconds, 10))
    while ((Get-Date) -lt $deadline) {
        $run = Invoke-CrossHedgeApi -Method "GET" -Path "/api/execution/probe-runs/$ProbeRunId"
        if ($run.status -eq "FLAT") { return $run }
        if ($run.status -in @("FAILED_NO_EXPOSURE", "RECOVERY_REQUIRED", "FAILED")) {
            throw "Probe #$ProbeRunId 进入异常终态: status=$($run.status), error=$($run.error_message)"
        }
        Start-Sleep -Seconds 1
    }
    throw "Probe #$ProbeRunId 在 $TimeoutSeconds 秒内未完成自动回平"
}

$evidence = [ordered]@{
    schema_version = 1
    started_at = (Get-Date).ToUniversalTime().ToString("o")
    base_url = $BaseUrl
    symbol = $Symbol
    status = "STARTING"
    credential = $null
    health_before = $null
    probes = @()
    health_after = $null
    error = ""
}

try {
    $credentials = @(Invoke-CrossHedgeApi -Method "GET" -Path "/api/settings/exchanges")
    $credential = $credentials | Where-Object { $_.venue -eq "binance" } | Select-Object -First 1
    if (-not $credential) { throw "未配置 Binance 凭证" }
    if (-not $credential.enabled -or -not $credential.configured) { throw "Binance 凭证未启用或未完整配置" }
    if ($credential.read_only) { throw "Binance 凭证为只读，无法执行 Testnet/Demo 验收" }
    $environment = ([string]$credential.environment).Trim().ToLowerInvariant()
    if ($environment -notin @("testnet", "demo")) {
        throw "安全拒绝：Binance environment=$environment，仅允许 testnet/demo，绝不在 live 执行此验收"
    }
    $evidence.credential = $credential

    $health = Invoke-CrossHedgeApi -Method "GET" -Path "/health"
    $evidence.health_before = $health
    $execRuntime = @($health.execution_worker.venue_runtimes) |
        Where-Object { $_.venue -eq "binance" } |
        Select-Object -First 1
    if ($health.status -ne "ok" -or -not $execRuntime -or -not $execRuntime.private_ws_connected) {
        throw "执行 Worker 未达到可交易健康状态"
    }
    if (([string]$execRuntime.environment).ToLowerInvariant() -ne $environment) {
        throw "凭证环境与执行 Runtime 环境不一致"
    }

    if ($DryRun) {
        $evidence.status = "DRY_RUN_OK"
        Write-Host "Dry-run 通过：环境=$environment，执行连接正常，未提交任何订单。"
    } else {
        if ($Confirmation -ne $ExpectedConfirmation) {
            throw "真实 Testnet/Demo 验收必须传 -Confirmation '$ExpectedConfirmation'"
        }
        foreach ($side in @("buy", "sell")) {
            # buy Probe 覆盖开多/平多，sell Probe 覆盖开空/平空；前一轮必须先完全 FLAT。
            $idempotencyKey = "acceptance:${environment}:${side}:${Timestamp}:$([guid]::NewGuid())"
            $created = Invoke-CrossHedgeApi `
                -Method "POST" `
                -Path "/api/execution/venue-probe-test" `
                -IdempotencyKey $idempotencyKey `
                -Body @{
                    symbol = $Symbol
                    venue = "binance"
                    side = $side
                    submit = $true
                    confirmation = "SUBMIT BINANCE PROBE"
                }
            $run = Wait-ProbeFlat -ProbeRunId ([int]$created.id)
            Assert-ProbeEvidence -Run $run -EntrySide $side
            $evidence.probes += $run
        }
        $evidence.status = "PASSED"
    }
} catch {
    $evidence.status = "FAILED"
    $evidence.error = $_.Exception.Message
    throw
} finally {
    try {
        $evidence.health_after = Invoke-CrossHedgeApi -Method "GET" -Path "/health"
    } catch {
        if (-not $evidence.error) { $evidence.error = "验收后健康检查失败: $($_.Exception.Message)" }
    }
    $evidence.completed_at = (Get-Date).ToUniversalTime().ToString("o")
    $directory = Split-Path -Parent $OutputPath
    New-Item -ItemType Directory -Force $directory | Out-Null
    $evidence | ConvertTo-Json -Depth 20 | Set-Content -Path $OutputPath -Encoding UTF8
    Write-Host "验收证据已写入: $OutputPath"
}

param(
    [int[]]$Ports = @(8000, 5173)
)

$ErrorActionPreference = "Continue"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunDir = Join-Path $Root ".run"
$PidFiles = @(
    (Join-Path $RunDir "backend.pid"),
    (Join-Path $RunDir "frontend.pid")
)

function Stop-ProcessTree($ProcessId) {
    if (-not $ProcessId) {
        return
    }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) {
        return
    }
    Write-Host "Stopping process tree PID=$ProcessId"
    taskkill /PID $ProcessId /T /F | Out-Null
}

foreach ($pidFile in $PidFiles) {
    if (Test-Path $pidFile) {
        $pidValue = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        Stop-ProcessTree $pidValue
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

foreach ($port in $Ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($conn in $connections) {
        Stop-ProcessTree $conn.OwningProcess
    }
}

Write-Host "Project processes stopped."

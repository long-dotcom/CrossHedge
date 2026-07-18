param(
    [int]$BackendPort = 8000
)

$ErrorActionPreference = "Continue"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path $Root ".run\backend.pid"
$ExecutionWorkerPidFile = Join-Path $Root ".run\execution-worker.pid"

function Stop-ProcessTree([int]$ProcessId) {
    if ($ProcessId -le 0 -or -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        return
    }
    # uvicorn 由隐藏 PowerShell 子进程托管，必须结束整棵进程树才能释放端口。
    taskkill /PID $ProcessId /T /F | Out-Null
}

if (Test-Path $PidFile) {
    $ProcessId = [int](Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    Stop-ProcessTree $ProcessId
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

if (Test-Path $ExecutionWorkerPidFile) {
    $ExecutionWorkerProcessId = [int](Get-Content $ExecutionWorkerPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    Stop-ProcessTree $ExecutionWorkerProcessId
    Remove-Item $ExecutionWorkerPidFile -Force -ErrorAction SilentlyContinue
}

$listeners = Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    Stop-ProcessTree ([int]$listener.OwningProcess)
}

Write-Host "Backend stopped: port=$BackendPort"

param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$PythonCommand = "py -3.14"
$BackendDir = Join-Path $Root "backend"
$FrontendDir = Join-Path $Root "frontend"
$RunDir = Join-Path $Root ".run"
$BackendPidFile = Join-Path $RunDir "backend.pid"
$FrontendPidFile = Join-Path $RunDir "frontend.pid"
$LogDir = Join-Path $RunDir "logs"

function Assert-File($Path, $Message) {
    if (-not (Test-Path $Path)) {
        throw $Message
    }
}

function Test-PortInUse($Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

Assert-File (Join-Path $Root ".env") ".env not found. Run create_env.cmd first."
Assert-File (Join-Path $FrontendDir "node_modules") "Frontend dependencies not found. Run install_packages.cmd first."
& py -3.14 -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 14) else 1)" | Out-Null

New-Item -ItemType Directory -Force $RunDir, $LogDir | Out-Null

if (Test-PortInUse $BackendPort) {
    throw "Backend port $BackendPort is already in use. Run stop_project.cmd or close the process manually."
}
if (Test-PortInUse $FrontendPort) {
    throw "Frontend port $FrontendPort is already in use. Run stop_project.cmd or close the process manually."
}

$BackendArgs = @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Set-Location '$BackendDir'; `$env:PYTHONPATH='.'; $PythonCommand -m uvicorn app.main:app --reload --host 127.0.0.1 --port $BackendPort"
)
$BackendProcess = Start-Process powershell.exe -ArgumentList $BackendArgs -PassThru -WindowStyle Normal
$BackendProcess.Id | Set-Content $BackendPidFile

$FrontendArgs = @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Set-Location '$FrontendDir'; npm run dev -- --host 127.0.0.1 --port $FrontendPort"
)
$FrontendProcess = Start-Process powershell.exe -ArgumentList $FrontendArgs -PassThru -WindowStyle Normal
$FrontendProcess.Id | Set-Content $FrontendPidFile

Write-Host "Backend started: http://127.0.0.1:$BackendPort"
Write-Host "Frontend started: http://127.0.0.1:$FrontendPort"
Write-Host "PID files: $RunDir"

if (-not $NoBrowser) {
    Start-Sleep -Seconds 2
    Start-Process "http://127.0.0.1:$FrontendPort"
}

param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# 项目路径结构：
#   .venv/          - Python 虚拟环境
#   backend/        - 后端 Python 代码（app/main:app）
#   frontend/       - 前端代码
#   .env            - 环境变量配置（根目录）
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$BackendDir = Join-Path $Root "backend"
$FrontendDir = Join-Path $Root "frontend"
$RunDir = Join-Path $Root ".run"
$BackendPidFile = Join-Path $RunDir "backend.pid"
$ExecutionWorkerPidFile = Join-Path $RunDir "execution-worker.pid"
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

# 检查虚拟环境、.env 配置和前端依赖是否存在
Assert-File $VenvPython "Virtual environment not found. Run install_packages.cmd first."
Assert-File (Join-Path $Root ".env") ".env not found. Run create_env.cmd first."
Assert-File (Join-Path $FrontendDir "node_modules") "Frontend dependencies not found. Run install_packages.cmd first."

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
    # 使用虚拟环境中的 Python 启动 uvicorn 后端服务（开发模式 --reload）
    "Set-Location '$BackendDir'; & '$VenvPython' -m uvicorn app.main:app --reload --host 127.0.0.1 --port $BackendPort"
)
$BackendProcess = Start-Process powershell.exe -ArgumentList $BackendArgs -PassThru -WindowStyle Normal
$BackendProcess.Id | Set-Content $BackendPidFile

$ExecutionWorkerArgs = @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Set-Location '$BackendDir'; & '$VenvPython' -m app.execution.worker_main"
)
$ExecutionWorkerProcess = Start-Process powershell.exe -ArgumentList $ExecutionWorkerArgs -PassThru -WindowStyle Normal
$ExecutionWorkerProcess.Id | Set-Content $ExecutionWorkerPidFile

$FrontendArgs = @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Set-Location '$FrontendDir'; npm run dev -- --host 127.0.0.1 --port $FrontendPort"
)
$FrontendProcess = Start-Process powershell.exe -ArgumentList $FrontendArgs -PassThru -WindowStyle Normal
$FrontendProcess.Id | Set-Content $FrontendPidFile

Write-Host "Backend started: http://127.0.0.1:$BackendPort"
Write-Host "Execution worker started: PID=$($ExecutionWorkerProcess.Id)"
Write-Host "Frontend started: http://127.0.0.1:$FrontendPort"
Write-Host "PID files: $RunDir"

if (-not $NoBrowser) {
    Start-Sleep -Seconds 2
    Start-Process "http://127.0.0.1:$FrontendPort"
}

param(
    [string]$PythonPath = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

if (-not (Test-Path $PythonPath)) {
    throw "未找到 Python: $PythonPath，请先创建虚拟环境并安装 mt5_gateway/requirements.txt"
}

$env:PYTHONPATH = (Join-Path $ProjectRoot "backend")
& $PythonPath -m mt5_gateway.main

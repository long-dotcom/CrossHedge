param(
    [string]$PythonPath = ".\.venv\Scripts\python.exe",
    [string]$GatewayEnvFile = ".mt5-gateway.env"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Import-EnvFile([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content $Path) {
        $value = $line.Trim()
        if (-not $value -or $value.StartsWith("#") -or -not $value.Contains("=")) { continue }
        $name, $content = $value.Split("=", 2)
        [Environment]::SetEnvironmentVariable($name.Trim(), $content.Trim().Trim('"').Trim("'"), "Process")
    }
}

Import-EnvFile (Join-Path $ProjectRoot $GatewayEnvFile)

if (-not $env:REDIS_URL) { throw "REDIS_URL is required in $GatewayEnvFile." }
if (-not $env:REDIS_PASSWORD) { throw "REDIS_PASSWORD is required in $GatewayEnvFile." }

if (-not (Test-Path $PythonPath)) {
    throw "Python not found: $PythonPath. Create the virtual environment and install mt5_gateway/requirements.txt first."
}

$env:PYTHONPATH = (Join-Path $ProjectRoot "backend")
& $PythonPath -m mt5_gateway.main

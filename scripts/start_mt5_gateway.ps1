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

if (-not $env:REDIS_URL) {
    $RedisContainer = (& docker compose ps -q redis).Trim()
    if (-not $RedisContainer) {
        throw "Redis is not running. Run .\scripts\start_stack.ps1 first."
    }
    $PortBinding = (& docker port $RedisContainer 16379/tcp).Trim() | Select-Object -First 1
    if (-not $PortBinding) {
        throw "Unable to discover the Redis host port."
    }
    $RedisPort = ($PortBinding -split ':')[-1]
    $RedisPassword = (& docker compose exec -T redis sh -c 'cat /run/crosshedge-secrets/redis_password').Trim()
    if (-not $RedisPassword) {
        throw "Unable to read the Redis password."
    }
    $env:REDIS_URL = "redis://127.0.0.1:$RedisPort/0"
    $env:REDIS_PASSWORD = $RedisPassword
}

if (-not (Test-Path $PythonPath)) {
    throw "Python not found: $PythonPath. Create the virtual environment and install mt5_gateway/requirements.txt first."
}

$env:PYTHONPATH = (Join-Path $ProjectRoot "backend")
& $PythonPath -m mt5_gateway.main

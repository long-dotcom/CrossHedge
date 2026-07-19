param(
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SourceEnv = Join-Path $ProjectRoot ".env"
$RuntimeEnv = Join-Path $ProjectRoot ".runtime.env"

function Read-EnvValues([string]$Path) {
    $values = [ordered]@{}
    if (-not (Test-Path $Path)) { return $values }
    foreach ($line in Get-Content $Path) {
        $value = $line.Trim()
        if (-not $value -or $value.StartsWith("#") -or -not $value.Contains("=")) { continue }
        $name, $content = $value.Split("=", 2)
        $values[$name.Trim()] = $content.Trim()
    }
    return $values
}

function Test-PortAvailable([int]$Port) {
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        $listener.Stop()
        return $true
    } catch {
        return $false
    }
}

function Find-AvailablePort([int]$Preferred, [int[]]$Reserved) {
    for ($port = $Preferred; $port -le 65535; $port++) {
        if ($Reserved -contains $port) { continue }
        if (Test-PortAvailable $port) { return $port }
    }
    throw "No available port was found at or above $Preferred."
}

function Get-CurrentPublishedPort([string]$Service, [string]$ContainerPort) {
    $container = (& docker compose ps -q $Service 2>$null).Trim()
    if (-not $container) { return 0 }
    $binding = (& docker port $container "$ContainerPort/tcp" 2>$null | Select-Object -First 1)
    if (-not $binding) { return 0 }
    return [int](($binding.Trim() -split ':')[-1])
}

$values = Read-EnvValues $SourceEnv
$preferredAppPort = if ($values["APP_PORT"]) { [int]$values["APP_PORT"] } else { 8080 }
$preferredRedisPort = if ($values["REDIS_HOST_PORT"]) { [int]$values["REDIS_HOST_PORT"] } else { 16379 }
$currentAppPort = Get-CurrentPublishedPort "frontend" "80"
$currentRedisPort = Get-CurrentPublishedPort "redis" "16379"
$appPort = if ($preferredAppPort -eq $currentAppPort) { $preferredAppPort } else { Find-AvailablePort $preferredAppPort @() }
$redisPort = if ($preferredRedisPort -eq $currentRedisPort -and $currentRedisPort -ne $appPort) { $preferredRedisPort } else { Find-AvailablePort $preferredRedisPort @($appPort) }
$values["APP_PORT"] = $appPort
$values["REDIS_HOST_PORT"] = $redisPort

$lines = foreach ($item in $values.GetEnumerator()) { "$($item.Key)=$($item.Value)" }
# .runtime.env is ignored by Git. Replace it atomically to avoid partial writes.
$temporary = "$RuntimeEnv.tmp"
[System.IO.File]::WriteAllLines($temporary, $lines, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::Move($temporary, $RuntimeEnv, $true)

Set-Location $ProjectRoot
$arguments = @("compose", "--env-file", $RuntimeEnv, "up", "-d")
if (-not $NoBuild) { $arguments += "--build" }
& docker @arguments
if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed to start." }

Write-Host "Frontend: http://localhost:$appPort"
Write-Host "Redis: 127.0.0.1:$redisPort (localhost only, password enabled)"

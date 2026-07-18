param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# 项目路径结构：
#   .env.example    - 环境变量模板（根目录）
#   .env            - 环境变量配置（根目录，由本脚本创建）
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnvExample = Join-Path $Root ".env.example"
$EnvFile = Join-Path $Root ".env"

if (-not (Test-Path $EnvExample)) {
    throw ".env.example not found"
}

if ((Test-Path $EnvFile) -and (-not $Force)) {
    Write-Host ".env already exists. Not overwritten. To recreate it, run: .\scripts\create_env.ps1 -Force"
    exit 0
}

Copy-Item -Path $EnvExample -Destination $EnvFile -Force

$content = Get-Content $EnvFile -Raw
$content = $content -replace "QUOTE_SOURCE_MODE=paper", "QUOTE_SOURCE_MODE=live"
$content | Set-Content -Path $EnvFile -Encoding utf8

Write-Host ".env created. Fill wallet address, private key, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, and change JWT_SECRET."
Write-Host $EnvFile

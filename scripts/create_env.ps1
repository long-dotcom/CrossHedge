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

Write-Host ".env created. JWT、交易所加密密钥和 Redis 密码将在首次启动时自动生成。"
Write-Host "如需指定 MT5 登录参数，请复制 .mt5-gateway.env.example 为 .mt5-gateway.env。"
Write-Host $EnvFile

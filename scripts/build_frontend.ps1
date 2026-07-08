$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$FrontendDir = Join-Path $Root "frontend"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm not found. Install Node.js LTS first."
}

Set-Location $FrontendDir

if (-not (Test-Path "node_modules")) {
    Write-Host "node_modules not found. Installing frontend packages first..."
    npm install
}

Write-Host "Building frontend..."
npm run build
Write-Host "Frontend build completed: frontend\dist"

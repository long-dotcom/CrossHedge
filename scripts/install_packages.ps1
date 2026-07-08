param(
    [switch]$SkipFrontend,
    [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $Root "frontend"
$Requirements = Join-Path $Root "backend\requirements.txt"

function Test-Command($Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

Set-Location $Root

if (-not $SkipBackend) {
    if (-not (Test-Command "py")) {
        throw "Python launcher not found. Install Python 3.14: winget install --id Python.Python.3.14 -e"
    }

    if (-not (Test-Path $VenvPython)) {
        Write-Host "Creating Python 3.14 virtual environment: .venv"
        py -3.14 -m venv .venv
    }

    Write-Host "Installing backend packages..."
    & $VenvPython -m pip install --upgrade pip setuptools wheel
    & $VenvPython -m pip install -r $Requirements
    & $VenvPython -m pip show nautilus_trader MetaTrader5
}

if (-not $SkipFrontend) {
    if (-not (Test-Command "npm")) {
        throw "npm not found. Install Node.js LTS: winget install --id OpenJS.NodeJS.LTS -e"
    }

    Write-Host "Installing frontend packages..."
    Set-Location $FrontendDir
    npm install
}

Write-Host "Package installation completed."

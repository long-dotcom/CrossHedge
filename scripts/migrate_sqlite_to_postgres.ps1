param(
    [string]$Source = "",
    [string]$TargetUrl = "",
    [string]$BackupDir = "",
    [switch]$Replace,
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$SkipBackup
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $PSScriptRoot "migrate_sqlite_to_postgres.py"

if (-not (Test-Path $Python)) {
    throw "Virtual environment not found. Run .\scripts\install_packages.ps1 first."
}
if (-not (Test-Path $Script)) {
    throw "Migration script not found: $Script"
}

$argsList = @($Script)

if ($Source) {
    $resolvedSource = Resolve-Path $Source
    $argsList += @("--source", $resolvedSource)
}
if ($TargetUrl) {
    $argsList += @("--target-url", $TargetUrl)
}
if ($BackupDir) {
    $argsList += @("--backup-dir", $BackupDir)
}
if ($Replace) {
    $argsList += "--replace"
}
if ($Yes) {
    $argsList += "--yes"
}
if ($DryRun) {
    $argsList += "--dry-run"
}
if ($SkipBackup) {
    $argsList += "--skip-backup"
}

& $Python @argsList

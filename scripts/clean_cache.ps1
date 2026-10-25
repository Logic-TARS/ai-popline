# Cache cleanup (PowerShell)
# Preview: .\scripts\clean_cache.ps1
# Delete:  .\scripts\clean_cache.ps1 -Apply
#
# Wrapper around scripts/clean_cache.py. The same logic backs the web console
# settings center -> cache maintenance tab, so both entry points stay in sync.
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 without a BOM, and the other
# project scripts carry no BOM either.

param(
    [switch]$Apply,
    [switch]$IncludeBuild,
    [string]$Root,
    [string[]]$Extra,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $PSScriptRoot 'clean_cache.py'

$pythonExe = $null
foreach ($candidate in @((Join-Path $projectRoot '.venv\Scripts\python.exe'), 'python', 'py')) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $pythonExe = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($command) {
        $pythonExe = $command.Source
        break
    }
}
if (-not $pythonExe) {
    Write-Error 'No Python interpreter found (.venv\Scripts\python.exe, python, or py).' -ErrorAction Continue
    exit 2
}

$arguments = @($scriptPath)
if ($Root) { $arguments += @('--root', $Root) }
if ($Apply) { $arguments += '--apply' }
if ($IncludeBuild) { $arguments += '--include-build' }
if ($Json) { $arguments += '--json' }
foreach ($name in $Extra) { if ($name) { $arguments += @('--extra', $name) } }

Push-Location -LiteralPath $projectRoot
try {
    Write-Host ("=== cache cleanup ({0}) ===" -f $(if ($Apply) { 'apply' } else { 'dry run' })) -ForegroundColor Cyan
    Write-Host ("python: {0}" -f $pythonExe) -ForegroundColor DarkGray
    & $pythonExe @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($exitCode -eq 0) {
    Write-Host '[OK] cache cleanup finished.' -ForegroundColor Green
} else {
    Write-Host "[FAIL] cache cleanup exited with code $exitCode" -ForegroundColor Red
}
exit $exitCode
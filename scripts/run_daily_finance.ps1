param(
    [switch]$SkipPublish
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$uvExe = 'C:\Users\Family\.local\bin\uv.exe'
$logDir = Join-Path $projectRoot 'output\scheduler'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir ("finance-{0}.log" -f (Get-Date -Format 'yyyyMMdd'))
$financeAccount = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('6YeR6J6N56C05aOB5Lq6'))

$taskPath = Join-Path $logDir 'task.finance.daily.json'
$task = [ordered]@{
    description  = '生成每日金融视频(定时任务)'
    content_type = 'finance'
    publish      = (-not $SkipPublish)
    params       = [ordered]@{
        date        = 'auto'
        script_mode = '90_seconds'
        douyin_account = $financeAccount
        kuaishou_account = $financeAccount
        dry_run     = $false
    }
}
$json = $task | ConvertTo-Json -Depth 5
# The CLI reads task files as plain UTF-8; a BOM would break json.loads.
[System.IO.File]::WriteAllText($taskPath, $json, (New-Object System.Text.UTF8Encoding($false)))

Push-Location $projectRoot
try {
    & $uvExe run ai-pipeline run --task $taskPath 2>&1 |
        Tee-Object -FilePath $logPath -Append
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

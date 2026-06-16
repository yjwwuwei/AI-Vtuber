$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Missing venv python: $python"
    exit 1
}

function Get-AIVtuberProcess {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like "python*" -and (
            $_.CommandLine -like "*$root*webui.py*" -or
            $_.CommandLine -like "*$root*main.py*" -or
            $_.CommandLine -match '(^| )webui\.py( |$)' -or
            $_.CommandLine -match '(^| )main\.py( |$)'
        )
    }
}

$existing = Get-AIVtuberProcess
if ($existing) {
    Write-Host "AI-Vtuber processes already running:"
    $existing | Select-Object ProcessId, CommandLine | Format-List
} else {
    Write-Host "Starting webui.py..."
    $webuiPath = Join-Path $root "webui.py"
    Start-Process -FilePath $python -ArgumentList $webuiPath -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Seconds 2

    Write-Host "Starting main.py..."
    $mainPath = Join-Path $root "main.py"
    Start-Process -FilePath $python -ArgumentList $mainPath -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "Current processes:"
Get-AIVtuberProcess | Select-Object ProcessId, CommandLine | Format-List
Write-Host ""
Write-Host "Startup uses .venv python."

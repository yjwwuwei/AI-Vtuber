$ErrorActionPreference = "SilentlyContinue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like "python*" -and (
        $_.CommandLine -like "*$root*webui.py*" -or
        $_.CommandLine -like "*$root*main.py*" -or
        $_.CommandLine -match '(^| )webui\.py( |$)' -or
        $_.CommandLine -match '(^| )main\.py( |$)'
    )
}

if (-not $targets) {
    Write-Host "No AI-Vtuber main.py/webui.py process found."
    exit 0
}

foreach ($proc in $targets) {
    Stop-Process -Id $proc.ProcessId -Force
    Write-Host "Stopped process $($proc.ProcessId)"
}

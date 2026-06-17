$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Missing venv python: $python"
    exit 1
}

$webuiUrl = "http://127.0.0.1:8080"

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

function Stop-AIVtuberProcess {
    $procs = Get-AIVtuberProcess
    if (-not $procs) {
        return
    }

    Write-Host "Stopping stale AI-Vtuber processes..."
    foreach ($proc in $procs) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Host "Failed to stop PID $($proc.ProcessId): $($_.Exception.Message)"
        }
    }

    Start-Sleep -Seconds 2
}

function Wait-WebuiPort {
    param(
        [int]$Port = 8080,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $listening = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
            $_.LocalPort -eq $Port
        }
        if ($listening) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

Stop-AIVtuberProcess

Write-Host "Starting webui.py..."
$webuiPath = Join-Path $root "webui.py"
Start-Process -FilePath $python -ArgumentList $webuiPath -WorkingDirectory $root -WindowStyle Hidden

if (Wait-WebuiPort -Port 8080 -TimeoutSeconds 20) {
    Write-Host "WebUI launched: $webuiUrl"
    Start-Process $webuiUrl
} else {
    Write-Host "WebUI did not open port 8080 in time."
}

Write-Host ""
Write-Host "Current processes:"
Get-AIVtuberProcess | Select-Object ProcessId, CommandLine | Format-List
Write-Host ""
Write-Host "Startup uses .venv python."

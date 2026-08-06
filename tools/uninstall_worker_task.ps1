# Removes the always-on Atlas dispatch worker scheduled task and stops any running instance.
#   Usage:  powershell -ExecutionPolicy Bypass -File <repo>\tools\uninstall_worker_task.ps1
$ErrorActionPreference = 'Continue'
$taskName = 'AtlasWorker'

try { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue } catch {}
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Best-effort: kill any worker process still alive (matches the heartbeat pid).
try {
    $hb = Get-Content (Join-Path (Split-Path -Parent $PSScriptRoot) 'jobs\_worker.json') -ErrorAction Stop | ConvertFrom-Json
    if ($hb.pid -and (Get-Process -Id $hb.pid -ErrorAction SilentlyContinue)) {
        taskkill /F /T /PID $hb.pid 2>&1 | Out-Null
    }
} catch {}

Write-Host "[AtlasWorker] unregistered + stopped."

# Registers the Atlas dispatch worker as an always-on Windows scheduled task, so you never
# start it by hand. Runs at your logon, restarts itself on crash, and is launched immediately
# (no need to log out/in). Re-runnable: -Force replaces any existing definition.
#
#   Usage:  powershell -ExecutionPolicy Bypass -File <repo>\tools\install_worker_task.ps1
#
# No admin required - it runs as the current user, at normal privilege, only while you are
# logged on (the worker needs your interactive session for the model CLI anyway).
$ErrorActionPreference = 'Stop'

$taskName = 'AtlasWorker'
$wrapper  = Join-Path $PSScriptRoot 'run_worker.ps1'   # resolves from this script, so the repo can live anywhere

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# WATCHDOG: also re-fire every 5 minutes, indefinitely. With MultipleInstances=IgnoreNew a fire while the
# worker is alive is a harmless no-op; but if the whole wrapper process ever dies (e.g. the control-C exit
# that Task Scheduler's restart-on-failure did NOT catch), this brings it back within 5 min.
# Belt-and-suspenders with the supervisor loop inside run_worker.ps1.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition

# Keep-alive policy: restart on failure (effectively forever), no run-time cap, survive battery,
# and never run a 2nd copy (the worker also self-guards via its heartbeat).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'Atlas dispatch worker - watches jobs and runs dispatched Claude sessions. Always-on (Stage 4 autonomous loop). Manage via tools install/uninstall scripts.' | Out-Null

Write-Host "[AtlasWorker] registered (logon trigger, restart-on-failure, single-instance)."

Start-ScheduledTask -TaskName $taskName
Write-Host "[AtlasWorker] started now - no logout needed."
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State

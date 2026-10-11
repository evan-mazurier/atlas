# Registers the daily ledger anchor as a Windows scheduled task "AtlasLedgerAnchor" (23:55 every day).
# It runs tools\anchor_ledger.py, which appends the hash of the last line of each ledger day file to the
# private anchor repo (the clone named in anchor.json, or ATLAS_ANCHOR_REPO), commits and pushes. See docs\v2\anchor-setup.md.
# Run this ONLY after the anchor repo exists and is cloned.
#
#   Usage:  powershell -ExecutionPolicy Bypass -File tools\install_anchor_task.ps1
#
# No admin required: current user, only while logged on. -StartWhenAvailable runs a missed run at the next logon.
$ErrorActionPreference = 'Stop'

$taskName = 'AtlasLedgerAnchor'
$repo     = Split-Path -Parent $PSScriptRoot
$script   = Join-Path $repo 'tools\anchor_ledger.py'

$action = New-ScheduledTaskAction -Execute 'py.exe' -Argument "-3.12 `"$script`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At '23:55'
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'Atlas ledger outside anchor: appends the day-file last-line hashes to the private anchor repo and pushes.' | Out-Null

Write-Host "[AtlasLedgerAnchor] registered (daily 23:55, current user, only when logged on)."

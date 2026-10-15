# Registers the daily ledger anchor as a Windows scheduled task "AtlasLedgerAnchor" (21:00 every day).
# It runs tools\anchor_ledger.py, which appends the hash of the last line of each ledger day file to the
# public anchor repo (the clone named in anchor.json, or ATLAS_ANCHOR_REPO), commits and pushes. See docs\v2\anchor-setup.md.
# Run this ONLY after the anchor repo exists and is cloned.
#
#   Usage:  powershell -ExecutionPolicy Bypass -File tools\install_anchor_task.ps1
#
# No admin required: current user, only while logged on. -StartWhenAvailable runs a missed run at the next logon.
$ErrorActionPreference = 'Stop'

$taskName = 'AtlasLedgerAnchor'
$repo     = Split-Path -Parent $PSScriptRoot
$script   = Join-Path $repo 'tools\anchor_ledger.py'

# Full path to the py launcher: Task Scheduler resolves a bare 'py.exe' against the system PATH only, and a per-user
# Python install puts the launcher on the user PATH, so the task failed with 0x80070002 (file not found).
$py = (Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $py) { throw "py.exe (the Python launcher) not found on PATH; install Python 3.12 with the launcher first." }
& $py -3.12 --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "$py -3.12 failed: Python 3.12 is not available through the launcher." }

$action = New-ScheduledTaskAction -Execute $py -Argument "-3.12 `"$script`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At '21:00'
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'Atlas ledger outside anchor: appends the day-file last-line hashes to the public anchor repo and pushes.' | Out-Null

Write-Host "[AtlasLedgerAnchor] registered (daily 21:00, current user, only when logged on), interpreter $py."

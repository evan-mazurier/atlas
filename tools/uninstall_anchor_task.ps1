# Removes the daily ledger anchor scheduled task.
#   Usage:  powershell -ExecutionPolicy Bypass -File tools\uninstall_anchor_task.ps1
$ErrorActionPreference = 'Continue'
$taskName = 'AtlasLedgerAnchor'

try { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue } catch {}
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "[AtlasLedgerAnchor] unregistered."

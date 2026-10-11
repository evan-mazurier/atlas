@echo off
REM Atlas cockpit launcher - the double-clickable / any-shell front door to cockpit.ps1.
REM
REM Why a .cmd at all: Windows PowerShell 5.1 (the Start-menu one) ships Restricted, so a bare .ps1
REM won't run there. Execution policy does not apply to .cmd, and -ExecutionPolicy Bypass scopes the
REM exemption to THIS process only - so the cockpit opens without weakening the machine's policy.
REM Stays in the current console (never start /b): the server's log output must stay visible in the console (the hidden autostart uses pyw --no-open).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cockpit.ps1" %*

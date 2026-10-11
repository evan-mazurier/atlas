# Atlas cockpit launcher - the one command to open the cockpit by hand.
#
# Two paths, because the cockpit is a server AND a window:
#   - already listening on the port -> just open the window (a second server would die on the port)
#   - not running                   -> start it in THIS console, which auto-opens the window
# It stays in the current console on purpose, so the server's log output stays visible. Ctrl-C stops it.
# (The hidden autostart is separate: it runs pyw with --no-open.)
param(
    [int]$Port = 8765,
    [switch]$OpenOnly,   # never start a server; just point a window at $Port
    [switch]$NoOpen      # start the server but leave the window closed
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$url  = "http://localhost:$Port"

function Open-CockpitWindow {
    # Mirrors _open_cockpit() in cockpit/server.py: a dedicated Chrome app window on its own minimal
    # profile (no tab bar), Edge's app mode as the fallback, the OS default browser as the last resort.
    $prof = Join-Path $env:LOCALAPPDATA 'AtlasCockpitProfile'
    $exe = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $exe) { $exe = (Get-Command chrome, msedge -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
    if ($exe) { Start-Process $exe -ArgumentList "--app=$url", "--user-data-dir=$prof" }
    else      { Start-Process $url }
}

$listening = $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)

if ($listening -or $OpenOnly) {
    if ($listening) { Write-Host "[cockpit] already running on $url - opening the window" -ForegroundColor DarkGray }
    Open-CockpitWindow
    return
}

# The cockpit renders whatever branch the repository folder is on; parallel sessions work in worktrees, but say so
# out loud if the shared dir has been left on something other than main.
$branch = & git -C $repo rev-parse --abbrev-ref HEAD 2>$null
if ($branch -and $branch -ne 'main') {
    Write-Host "[cockpit] note: $repo is on '$branch', not main" -ForegroundColor Yellow
}

Write-Host "[cockpit] starting on $url  (Ctrl-C to stop)" -ForegroundColor DarkGray
$serverArgs = @('-3.12', (Join-Path $repo 'cockpit\server.py'), '--port', $Port)
if ($NoOpen) { $serverArgs += '--no-open' }
& py @serverArgs

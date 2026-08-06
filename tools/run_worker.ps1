# Atlas dispatch worker launcher - invoked by the "AtlasWorker" scheduled task.
# Keeps the worker's stdout/stderr in a rolling log so a headless (windowless) run is debuggable.
#
# SUPERVISOR LOOP: the worker is itself a forever loop, but if it ever exits or crashes we relaunch it
# OURSELVES rather than relying solely on Task Scheduler's restart-on-failure - which once missed a
# `0xC000013A` (control-C) exit and left the worker dead for hours. This wrapper stays alive as the task
# instance and re-spawns the worker with a short backoff. Belt; the task's repetition trigger (see
# install_worker_task.ps1) is the suspenders for when this wrapper process itself dies.
#
# Paths resolve from THIS script's location, so the repo can live anywhere.
$ErrorActionPreference = 'Continue'
$env:PYTHONUNBUFFERED = '1'

$repo = Split-Path -Parent $PSScriptRoot
$log  = Join-Path $repo 'logs\worker.log'

New-Item -ItemType Directory -Force (Join-Path $repo 'logs') | Out-Null

function Roll-Log {
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) { Move-Item $log "$log.1" -Force }
}

while ($true) {
    Roll-Log
    "[run_worker] launching worker at $(Get-Date -Format s)" *>> $log
    & py -3.12 (Join-Path $repo 'dashboard\worker.py') *>> $log
    $code = $LASTEXITCODE
    "[run_worker] worker exited (code=$code) at $(Get-Date -Format s) - relaunching in 5s" *>> $log
    Start-Sleep -Seconds 5
}

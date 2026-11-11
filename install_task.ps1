param(
    # -Auto : non-interactive mode, used by ONE_CLICK_INSTALL.bat.
    #   Skips every 'Press Enter' prompt and fires the task once at the end
    #   so the user immediately sees whether it actually works.
    [switch]$Auto
)

# ===========================================================================
#  install_task.ps1 ---- register the weekday 16:30 scheduled task
# ---------------------------------------------------------------------------
#  Why PowerShell instead of schtasks:
#    schtasks.exe CANNOT do these two things no matter how you quote it:
#      1. set the task's "Start in" (working directory)
#      2. set "Run task as soon as possible after a scheduled start is missed"
#    Register-ScheduledTask does both, in one shot, with no quote hell.
#
#  HOW TO RUN (must be elevated):
#    Right-click Start -> "Terminal (Admin)" / "Windows PowerShell (Admin)", then:
#        cd C:\Users\20290\WorkBuddy\2026-11-10-16-55-15\a_share_review
#        powershell -ExecutionPolicy Bypass -File .\install_task.ps1
#
#  If you see "cannot be loaded because running scripts is disabled",
#  the -ExecutionPolicy Bypass above already handles it.
#  Do NOT change the machine-wide policy just for this.
#
#  NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 as ANSI;
#  a UTF-8 file with Chinese comments shows garbled text.
# ===========================================================================

$ErrorActionPreference = 'Stop'

$TaskName = 'AStock_Review_1630'
$ProjectDir = $PSScriptRoot
$VbsPath    = Join-Path $ProjectDir 'run_silent.vbs'
$WscriptExe = Join-Path $env:SystemRoot 'System32\wscript.exe'

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  Register scheduled task: weekday 16:30' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

# ---- 1. elevation check -------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host '[STOP] Not elevated.' -ForegroundColor Red
    Write-Host '       Close this window, open "Terminal (Admin)", and run again.'
    if (-not $Auto) { Read-Host 'Press Enter to exit' }
    exit 1
}
Write-Host '[OK] Elevated.' -ForegroundColor Green

# ---- 2. pre-checks ------------------------------------------------------
foreach ($f in @($VbsPath, $WscriptExe)) {
    if (-not (Test-Path -LiteralPath $f)) {
        Write-Host "[FAIL] Not found: $f" -ForegroundColor Red
        if (-not $Auto) { Read-Host 'Press Enter to exit' }
        exit 1
    }
    Write-Host "[OK] $f" -ForegroundColor Green
}
Write-Host ''

# ---- 3. build action / trigger / settings / principal -------------------
# Argument is single-quoted on purpose: the inner double quotes become
# literal characters handed to wscript.exe, which is exactly what we want.
$Action = New-ScheduledTaskAction `
    -Execute $WscriptExe `
    -Argument ('"{0}"' -f $VbsPath) `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At '16:30'

# Parameter names verified against this machine's ScheduledTasks module:
#   New-ScheduledTaskSettingsSet has NO -DisallowStartIfOnBatteries.
#   The switches are inverted:
#       -AllowStartIfOnBatteries   => DisallowStartIfOnBatteries = false
#       -DontStopIfGoingOnBatteries => StopIfGoingOnBatteries   = false
#   Using the wrong name throws "parameter cannot be found" and the
#   whole registration dies before anything is created.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Write-Host 'Action  :' $WscriptExe $Action.Arguments
Write-Host 'StartIn :' $ProjectDir
Write-Host 'Trigger : Mon-Fri 16:30'
Write-Host 'Settings: StartWhenAvailable = ON, battery = allowed, no stop on battery'
Write-Host ''

# ---- 4. register --------------------------------------------------------
try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description 'A-share daily review: generate report and push to GitHub Pages' `
        -Force | Out-Null
    Write-Host '[OK] Task registered.' -ForegroundColor Green
}
catch {
    Write-Host '[FAIL] Registration failed:' -ForegroundColor Red
    Write-Host $_.Exception.Message
    if (-not $Auto) { Read-Host 'Press Enter to exit' }
    exit 1
}

# ---- 5. verify ----------------------------------------------------------
Write-Host ''
Write-Host '--- Verification -------------------------------------------'
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $t) {
    Write-Host '[FAIL] Task not found after registration.' -ForegroundColor Red
    if (-not $Auto) { Read-Host 'Press Enter to exit' }
    exit 1
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ('  State        : {0}' -f $t.State)
Write-Host ('  Action       : {0} {1}' -f $t.Actions[0].Execute, $t.Actions[0].Arguments)
Write-Host ('  StartIn      : {0}' -f $t.Actions[0].WorkingDirectory)
Write-Host ('  NextRunTime  : {0}' -f $info.NextRunTime)
Write-Host ('  StartWhenAvailable : {0}' -f $t.Settings.StartWhenAvailable)
Write-Host ''

Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  DONE. Test it now without waiting until 16:30:' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
Write-Host ''
Write-Host '  Then check reports\ and logs\review.log'
Write-Host ''
Write-Host '  Remove it later with:'
Write-Host "    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
Write-Host ''
if ($Auto) {
    Write-Host '--- Firing the task once to verify -------------------------'
    try {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host '[OK] Task started in the background.' -ForegroundColor Green
        Write-Host '     Watch reports\ and logs\review.log for the result.'
        Write-Host '     A browser tab opens when the review finishes.'
    }
    catch {
        Write-Host '[WARN] Could not start it right now:' -ForegroundColor Yellow
        Write-Host $_.Exception.Message
        Write-Host '       The task IS registered; run it from Task Scheduler later.'
    }
    Start-Sleep -Seconds 3
}
else {
    Read-Host 'Press Enter to exit'
}

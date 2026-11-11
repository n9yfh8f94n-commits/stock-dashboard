@echo off
rem ===========================================================================
rem  ONE_CLICK_INSTALL.bat  ---  double-click this file, that is all.
rem ---------------------------------------------------------------------------
rem  What it does:
rem    1. detects it is not elevated, relaunches itself with a UAC prompt
rem    2. after you click YES, runs install_task.ps1 -Auto
rem    3. that registers the task AStock_Review_1630 (Mon-Fri 16:30) with:
rem         program   : wscript.exe
rem         argument  : run_silent.vbs      (absolute path)
rem         start in  : the project folder   <-- set explicitly
rem         missed run: catch up ASAP        <-- schtasks cannot do this
rem         battery   : allowed              <-- schtasks cannot do this
rem    4. fires the task once so you can verify, then closes itself
rem
rem  Why the file name is ENGLISH, not Chinese:
rem  Elevation passes %~f0 into Start-Process. A Chinese file name can be
rem  mangled during the ANSI -> Unicode handoff, and then the elevated window
rem  never appears - the classic "I clicked Yes but nothing happened".
rem  An ASCII file name removes that failure mode completely.
rem
rem  NOTE: keep this file pure ASCII. A UTF-8 .bat with Chinese text is parsed
rem  under the GBK code page and hangs. Verified earlier in this project.
rem ===========================================================================

setlocal EnableExtensions
cd /d "%~dp0"
title A-Share Review - One Click Installer

rem ---------------------------------------------------------------------------
rem  Elevation gate
rem ---------------------------------------------------------------------------
net session >nul 2>&1
if errorlevel 1 goto :elevate
goto :main

:elevate
echo.
echo   ============================================================
echo    Administrator privileges required
echo   ============================================================
echo.
echo    A UAC prompt will appear in a second.
echo    Click YES, and a new window takes over automatically.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
if errorlevel 1 (
    echo.
    echo    [FAILED] Elevation was refused or blocked.
    echo.
    echo    Fallback: right-click this file and choose
    echo              "Run as administrator"
    echo.
    pause
)
rem Give the elevated copy a moment to start, then close this one.
timeout /t 2 >nul
exit /b

rem ---------------------------------------------------------------------------
rem  Main
rem ---------------------------------------------------------------------------
:main
echo.
echo   ============================================================
echo    A-Share Review : task installer
echo   ============================================================
echo.
echo    Project : %~dp0
echo.

set "PS1=%~dp0install_task.ps1"
if not exist "%PS1%" goto :no_ps1
if not exist "%~dp0run_silent.vbs" goto :no_vbs

echo    Running installer...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Auto
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" goto :failed

echo   ============================================================
echo    DONE
echo   ============================================================
echo.
echo    Task     : AStock_Review_1630
echo    Program  : wscript.exe
echo    Argument : run_silent.vbs
echo    Start in : %~dp0
echo    Schedule : Monday-Friday 16:30
echo    Missed   : run as soon as possible
echo    Battery  : allowed
echo.
echo    It was fired once just now to verify.
echo    A browser tab should open in about a minute.
echo    Then check:  %~dp0reports\
echo.
echo    Open Task Scheduler to confirm:
echo       taskschd.msc
echo.
timeout /t 6 >nul
exit /b 0

rem ===========================================================================
:no_ps1
echo.
echo   [FAILED] install_task.ps1 is missing next to this file:
echo            %PS1%
echo.
echo   Keep these three files together in a_share_review:
echo       ONE_CLICK_INSTALL.bat
echo       install_task.ps1
echo       run_silent.vbs
echo.
pause
exit /b 1

:no_vbs
echo.
echo   [FAILED] run_silent.vbs is missing:
echo            %~dp0run_silent.vbs
echo.
pause
exit /b 1

:failed
echo   ============================================================
echo    FAILED  (exit code %RC%)
echo   ============================================================
echo.
echo    The task was NOT created. Checklist:
echo.
echo      1. Did you click YES on the UAC prompt?
echo         If not, right-click this file - Run as administrator
echo.
echo      2. Execution policy blocked the script?
echo         Run this by hand to see the real error:
echo           powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
echo.
echo      3. Antivirus / endpoint protection blocked task creation?
echo         Try adding the project folder to the exclusion list.
echo.
echo      4. Did you delete the task earlier and it is still locked?
echo         Check:  taskschd.msc
echo.
pause
exit /b %RC%

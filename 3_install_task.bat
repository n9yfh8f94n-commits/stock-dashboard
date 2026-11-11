@echo off
rem ===========================================================================
rem  Script 3 of 3 : register the weekday 16:30 scheduled task
rem ---------------------------------------------------------------------------
rem  IMPORTANT: right-click this file and choose "Run as administrator".
rem  Without elevation schtasks returns "Access is denied".
rem  This script refuses to continue if you are not elevated, instead of
rem  failing halfway through.
rem
rem  Task name is ASCII on purpose ("AStock_Review_1630"). A Chinese task name
rem  would require this .bat to contain non-ASCII bytes, and a UTF-8 .bat is
rem  parsed as GBK by cmd.exe - the script breaks and hangs. Verified.
rem
rem  NOTE: schtasks.exe has NO command-line switch for
rem        "Run task as soon as possible after a scheduled start is missed".
rem  This script creates the task, then tells you exactly which two checkboxes
rem  to tick in the GUI. That part cannot be automated from the command line.
rem ===========================================================================

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   INSTALL 3 : weekday 16:30 scheduled task
echo ============================================================
echo.

rem ---------------------------------------------------------------
rem  Step 0 : require elevation
rem ---------------------------------------------------------------
net session >nul 2>&1
if errorlevel 1 goto :need_admin
echo   [OK] Running with administrator privileges.
echo.

rem ---------------------------------------------------------------
rem  Step 1 : pre-checks
rem ---------------------------------------------------------------
set "VBS=%~dp0run_silent.vbs"
set "TASKNAME=AStock_Review_1630"

if not exist "%VBS%" goto :no_vbs
if not exist "%~dp0daily.bat" goto :no_bat
echo   [OK] run_silent.vbs
echo   [OK] daily.bat
echo.
echo   Will register:
echo     Task name : %TASKNAME%
echo     Runs      : wscript.exe "%VBS%"
echo     Schedule  : Mon-Fri 16:30
echo     WorkingDir: %~dp0
echo.

rem ---------------------------------------------------------------
rem  Step 2 : create / overwrite the task
rem ---------------------------------------------------------------
echo ------------------------------------------------------------
echo   Registering...
echo ------------------------------------------------------------
schtasks /Create /TN "%TASKNAME%" /TR "wscript.exe \"%VBS%\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:30 /RL LIMITED /F
if errorlevel 1 goto :create_failed
echo.

rem ---------------------------------------------------------------
rem  Step 3 : verify it exists
rem ---------------------------------------------------------------
schtasks /Query /TN "%TASKNAME%" /FO LIST | findstr /I "TaskName Status Next NextRunTime"
echo.

echo ============================================================
echo   [DONE] Task registered.
echo ============================================================
echo.
echo   ONE MANUAL STEP REMAINS (schtasks cannot set it):
echo.
echo   1. Press Win+R, type  taskschd.msc  and hit Enter
echo   2. Find "%TASKNAME%" in the list, right-click - Properties
echo   3. On the "Settings" tab, tick:
echo        [x] Run task as soon as possible after a scheduled start is missed
echo        [ ] Start the task only if the computer is on AC power   <-- UNTICK
echo   4. Click OK
echo.
echo   Step 3 prevents a missed run when the laptop was asleep at 16:30.
echo   Step 3b lets it run on battery.
echo.
echo   Test it now without waiting until 16:30:
echo     taskschd.msc -^> right-click the task -^> Run
echo     then check reports\ and logs\review.log
echo.
echo   Remove the task later with:
echo     schtasks /Delete /TN "%TASKNAME%" /F
echo.
pause
exit /b 0

rem ===============================================================
:need_admin
echo ------------------------------------------------------------
echo   [STOP] Administrator privileges required.
echo ------------------------------------------------------------
echo.
echo   schtasks will fail with "Access is denied" without elevation.
echo.
echo   How to run elevated:
echo     1. Close this window
echo     2. Right-click 3_install_task.bat
echo     3. Choose "Run as administrator"
echo     4. Click Yes on the UAC prompt
echo.
pause
exit /b 1

:no_vbs
echo [ERROR] run_silent.vbs not found in %~dp0
echo         Keep all three scripts inside the a_share_review folder.
pause
exit /b 1

:no_bat
echo [ERROR] daily.bat not found in %~dp0
pause
exit /b 1

:create_failed
echo.
echo ------------------------------------------------------------
echo   [FAILED] schtasks returned an error.
echo ------------------------------------------------------------
echo.
echo   Exit code: %ERRORLEVEL%
echo.
echo   Interpret the error:
echo     "Access is denied"        -^> you are not elevated; right-click,
echo                                   Run as administrator
echo     "Invalid argument"        -^> a path contains a special character;
echo                                   move the project to a simpler folder
echo     "The task already exists" -^> should not happen thanks to /F
echo.
echo   Or create it by hand:
echo     taskschd.msc -^> Create Basic Task
echo     Program : wscript.exe
echo     Arg     : "%VBS%"
echo     Start in: %~dp0
echo.
pause
exit /b 1

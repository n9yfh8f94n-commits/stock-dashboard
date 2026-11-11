@echo off
rem ===========================================================================
rem  A-share daily review - one-click runner
rem ---------------------------------------------------------------------------
rem  Usage:
rem      daily.bat              review today and publish
rem      daily.bat 20261109     review a specific date
rem      daily.bat --no-push    local only, skip GitHub Pages publish
rem
rem  Steps:
rem      1/3  main.py       -> reports/*.md       (report + metrics history)
rem      2/3  dashboard.py  -> reports/*.html     (charts + open browser)
rem      3/3  deploy.py     -> docs/index.html    (git push to GitHub Pages)
rem
rem  Order matters: dashboard.py reads calibration/metrics_history.csv,
rem  which is written by main.py. deploy.py reads the html from step 2.
rem  Running them out of order means the history file never grows.
rem
rem  NOTE: keep this file pure ASCII. A UTF-8 .bat with Chinese text is
rem  parsed under the GBK code page, the comment lines break apart and
rem  get executed as commands ("'xxx' is not recognized..."). Verified:
rem  it hangs for minutes instead of exiting. Do not add Chinese here.
rem ===========================================================================

setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem ===========================================================================
rem  INTRADAY GUARD  - must be the FIRST action, before anything reads/writes data
rem ---------------------------------------------------------------------------
rem  2026-11-11 13:06 accident: a desktop shortcut ran during trading hours,
rem  a half-day snapshot was written into metrics_history.csv and pushed live.
rem
rem  guard_popup.py reuses utils.guard_intraday (single source of truth) and
rem  shows a Chinese MessageBox. Exit codes:
rem      0 = proceed
rem      2 = blocked. We exit with 0 on purpose so run_silent.vbs does NOT
rem          pop a second "Run failed" dialog - the guard message is enough.
rem ===========================================================================
python "%~dp0guard_popup.py" %*
set "GRC=%ERRORLEVEL%"
if "%GRC%"=="2" exit /b 0
if not "%GRC%"=="0" (
    echo [ERROR] Intraday guard script failed with code %GRC%.
    echo         Nothing was run. Check logs\review.log.
    pause
    exit /b 1
)

rem ---- optional switch: --no-push skips step 3 ----
set DOPUSH=1
set PUSHOK=1
if /i "%~1"=="--no-push" (
    set DOPUSH=0
    shift
)

if "%~1"=="" goto :today

echo [1/3] main.py  date=%~1
python main.py %~1
if errorlevel 1 goto :fail_main

echo.
echo [2/3] dashboard.py  date=%~1
python dashboard.py %~1
if errorlevel 1 goto :fail_dash
goto :deploy

:today
echo [1/3] main.py  date=today
python main.py
if errorlevel 1 goto :fail_main

echo.
echo [2/3] dashboard.py  date=today
python dashboard.py
if errorlevel 1 goto :fail_dash
goto :deploy

:deploy
if "%DOPUSH%"=="0" (
    echo.
    echo [3/3] deploy.py skipped ^(--no-push^)
    goto :done
)
if not exist "deploy.py" (
    echo.
    echo [3/3] deploy.py not found, skipped
    goto :done
)

echo.
echo [3/3] deploy.py  publish to GitHub Pages
python deploy.py
if errorlevel 1 (
    set PUSHOK=0
    echo.
    echo [WARN] Publish failed, but local reports are already generated.
    echo        Fix network / GitHub credentials, then run: python deploy.py
    echo        Local files are NOT affected.
)
goto :done

:fail_main
echo.
echo [FAILED] main.py returned an error. See logs\review.log
endlocal
exit /b 1

:fail_dash
echo.
echo [FAILED] dashboard.py returned an error.
echo          The markdown report is still available in reports\
echo          See logs\review.log
endlocal
exit /b 1

:done
echo.
echo ===========================================================
echo  Done. Local files in reports\ :
echo    the .md    text report
echo    the .html  dashboard ^(opened in browser^)
if "%DOPUSH%"=="1" if "%PUSHOK%"=="1" echo  Published to docs\index.html
if "%DOPUSH%"=="1" if "%PUSHOK%"=="0" echo  NOT published ^(see messages above^)
echo ===========================================================
endlocal
exit /b 0

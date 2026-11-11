@echo off
rem ===========================================================================
rem  Script 1 of 3 : test the silent launcher
rem  Target : run_silent.vbs (which calls daily.bat with its window hidden)
rem
rem  Double-click this file. It runs a FULL review for 2026-11-09 and pushes
rem  to GitHub, then shows you the exit code and the tail of the log.
rem
rem  NOTE: keep this file pure ASCII. A UTF-8 .bat with Chinese text is parsed
rem  under the GBK code page and hangs in a loop. This has been verified.
rem ===========================================================================

setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo   TEST 1 : silent launcher (run_silent.vbs)
echo ============================================================
echo.
echo   It will run a FULL review for 2026-11-09 and push to GitHub.
echo   Takes about 40 seconds. The browser opens at the end.
echo.
echo   Why a past date instead of today?
echo   Today is still trading hours. Writing a half-day snapshot into
echo   metrics_history.csv would silently corrupt every later
echo   percentile calibration, and the numbers would still look normal.
echo.

echo ------------------------------------------------------------
echo   Pre-checks
echo ------------------------------------------------------------
set "MISSING=0"
if exist "run_silent.vbs" (echo   [OK]   run_silent.vbs) else (echo   [FAIL] run_silent.vbs missing & set "MISSING=1")
if exist "daily.bat"      (echo   [OK]   daily.bat)      else (echo   [FAIL] daily.bat missing & set "MISSING=1")
if exist "app.ico"        (echo   [OK]   app.ico)        else (echo   [warn] app.ico missing - cosmetic only, continue)
if exist "docs\"          (echo   [OK]   docs\ exists, Pages can serve it) else (echo   [warn] docs\ missing - run: python deploy.py)
echo.
if "%MISSING%"=="1" goto :missing_files

echo   Python check:
python -c "import akshare, pyecharts; print('   [OK]   akshare + pyecharts ready')" 2>nul
if errorlevel 1 (
    echo   [FAIL] python or packages missing
    echo          Try:  python -c "import akshare"
    echo          If it fails:  python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo.

echo ------------------------------------------------------------
echo   Running wscript.exe run_silent.vbs 20261109
echo   (the console window of daily.bat stays hidden on purpose)
echo ------------------------------------------------------------
echo.
start "" /wait wscript.exe "run_silent.vbs" 20261109
set "RC=%ERRORLEVEL%"

echo.
echo ------------------------------------------------------------
echo   Exit code = %RC%
echo ------------------------------------------------------------
if "%RC%"=="0" goto :success

echo   [FAILED] The review or the push did not complete.
echo.
echo   Most common causes, in order of likelihood:
echo     1. Missing python packages
echo          python -c "import akshare; import pyecharts"
echo     2. No network / GitHub auth expired
echo          try:  git push
echo     3. Eastmoney API temporarily down
echo.
echo   Last 12 lines of logs\review.log:
echo   --------------------------------------------------------
if exist "logs\review.log" (
    powershell -NoProfile -Command "Get-Content -LiteralPath 'logs\review.log' -Tail 12" 2>nul
    if errorlevel 1 echo   (could not run powershell, open the file manually)
) else (
    echo   [warn] logs\review.log not found - did daily.bat ever run?
)
echo   --------------------------------------------------------
echo.
echo   Full log: %CD%\logs\review.log
echo.
pause
exit /b %RC%

:success
echo   [SUCCESS]
echo.
echo   What should have happened:
echo     1. Browser opened reports\dashboard_20261109.html
echo     2. reports\ has a fresh .md and .html
echo     3. GitHub got a new commit "Auto deploy 2026-11-09"
echo.
echo   Check the repo:
echo     https://github.com/n9yfh8f94n-commits/stock-dashboard/commits/main
echo.
echo   If the browser did NOT open but exit code is 0, the charts were
echo   still generated - just open reports\dashboard_20261109.html yourself.
echo.
echo   Next: run 2_create_shortcut.vbs to get a desktop icon.
echo.
pause
exit /b 0

:missing_files
echo ------------------------------------------------------------
echo   [ABORTED] Required files are missing.
echo   Make sure all three scripts sit in the a_share_review folder,
echo   next to daily.bat.
echo ------------------------------------------------------------
echo.
pause
exit /b 1

@echo off
rem ===========================================================================
rem  ROLLBACK_20261111.bat
rem  One-click recovery from the 2026-11-11 intraday pollution.
rem
rem  What happened at 13:06 that day:
rem    a half-day snapshot was written to metrics_history.csv and
rem    docs/index.html was pushed to GitHub Pages.
rem
rem  What this script does:
rem    [1/5] run fix_pollution.py --day 20261111 (idempotent; safe to rerun)
rem    [2/5] make sure no dashboard_20261111.html survives in reports\
rem    [3/5] rebuild the dashboard for 20261110 (last COMPLETE trading day)
rem    [4/5] deploy.py copies it to docs\index.html and pushes to GitHub
rem    [5/5] verify the published page banner says 2026-11-10
rem
rem  IMPORTANT: this file is pure ASCII on purpose.
rem  A UTF-8 .bat with Chinese text is parsed as GBK by cmd.exe and hangs.
rem ===========================================================================

setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo   ============================================================
echo    ROLLBACK : remove 2026-11-11 intraday pollution
echo   ============================================================
echo.

rem ---------------------------------------------------------------------------
rem  Step 1 : clean dirty data (idempotent)
rem ---------------------------------------------------------------------------
echo [1/5] Cleaning dirty data for 20261111 ...
python fix_pollution.py --day 20261111
if errorlevel 1 goto :fail
echo.

rem ---------------------------------------------------------------------------
rem  Step 2 : no stale 11-11 dashboard may survive, or deploy.py will pick it
rem ---------------------------------------------------------------------------
echo [2/5] Checking for a stale 11-11 dashboard ...
if exist "reports\dashboard_20261111.html" (
    echo       found one - renaming to .dirty.bak so deploy cannot select it
    move /Y "reports\dashboard_20261111.html" "reports\dashboard_20261111.html.dirty.bak" >nul
) else (
    echo       none found - OK
)
echo.

rem ---------------------------------------------------------------------------
rem  Step 3 : rebuild for 20261110 (full cache exists for that day)
rem ---------------------------------------------------------------------------
echo [3/5] Rebuilding dashboard for 20261110 ...
python dashboard.py 20261110 --no-open
if errorlevel 1 goto :fail
if not exist "reports\dashboard_20261110.html" goto :no_html
echo       generated reports\dashboard_20261110.html
echo.

rem ---------------------------------------------------------------------------
rem  Step 4 : publish (overwrites docs\index.html and pushes)
rem ---------------------------------------------------------------------------
echo [4/5] Publishing to GitHub Pages ...
python deploy.py
set "DEPLOY_RC=%ERRORLEVEL%"
if not "%DEPLOY_RC%"=="0" (
    echo.
    echo   [WARN] deploy.py returned %DEPLOY_RC%.
    echo          Local files are correct; the push may have failed.
    echo          Re-run later with:  python deploy.py
    echo.
    pause
    exit /b %DEPLOY_RC%
)
echo.

rem ---------------------------------------------------------------------------
rem  Step 5 : verify the published banner
rem ---------------------------------------------------------------------------
echo [5/5] Verifying the published page and history file ...
python verify_page.py
if errorlevel 1 goto :fail
echo.

echo   ============================================================
echo    DONE
echo   ============================================================
echo.
echo    The page now shows 2026-11-10 data with the full 30-day history.
echo    GitHub Pages may need 1-3 minutes to refresh its CDN.
echo.
echo    Final URL:
echo      https://n9yfh8f94n-commits.github.io/stock-dashboard/
echo.
echo    Also verify the history file is clean (done in step 5 above).
echo.
pause
exit /b 0

rem ===========================================================================
:no_html
echo.
echo   [FAILED] dashboard_20261110.html was not generated.
echo            Check cache\20261110_* files and logs\review.log
echo.
pause
exit /b 1

:fail
echo.
echo   ============================================================
echo    FAILED - nothing further was executed.
echo   ============================================================
echo    Run these by hand to see the real error:
echo      python fix_pollution.py --day 20261111
echo      python dashboard.py 20261110
echo      python deploy.py
echo.
pause
exit /b 1

@echo off
setlocal enabledelayedexpansion

REM ===========================================================================
REM Tune Server — Windows launcher with pre-flight checks and crash UX.
REM ===========================================================================

cd /d "%~dp0"

echo.
echo === Tune Server ===
echo Working directory: %cd%
echo.

REM ---------------------------------------------------------------------------
REM Pre-flight 1: required binary exists
REM ---------------------------------------------------------------------------
if not exist "tune-server.exe" (
    echo [ERROR] tune-server.exe is missing in this folder.
    echo The install zip extraction was incomplete or corrupted.
    echo Re-download from https://github.com/renesenses/tune-server-linux/releases
    echo.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM Pre-flight 2: ffmpeg.exe needed for transcoding (optional but warn)
REM ---------------------------------------------------------------------------
if not exist "ffmpeg.exe" (
    echo [WARN] ffmpeg.exe missing — audio transcoding will be unavailable.
    echo Some streaming services (Tidal, Qobuz, etc.) may fail.
    echo Re-extract the zip to restore it.
    echo.
)

REM ---------------------------------------------------------------------------
REM Pre-flight 3: port 8888 must be free
REM ---------------------------------------------------------------------------
netstat -ano 2>nul | findstr ":8888 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [ERROR] Port 8888 is already in use on this machine.
    echo Another program ^(maybe a previous Tune Server instance^) is bound to
    echo the port. Close it from Task Manager, then run this launcher again.
    echo.
    netstat -ano 2>nul | findstr ":8888 " | findstr "LISTENING"
    echo.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM Browser auto-open: poll /api/v1/system/health, open browser when ready.
REM Runs in a sibling helper script so we don't have to escape && / || / ()
REM through a `cmd /c "..."` quoted string — that escaping breaks on French
REM Windows locales with the cryptic 'may était inattendu' error.
REM ---------------------------------------------------------------------------
start "" /B "%~dp0_open_browser_when_ready.bat"

REM ---------------------------------------------------------------------------
REM Run the server with watchdog. On crash (non-zero exit), auto-restart up
REM to MAX_ATTEMPTS times with backoff before giving up. A clean exit
REM (code 0, e.g. user closed window or auto-update handed off) is final.
REM ---------------------------------------------------------------------------
set MAX_ATTEMPTS=3
set ATTEMPT=1

echo Starting Tune Server...
echo Web UI will open at http://localhost:8888 once ready.
echo Logs are written to: %cd%\tune-server.log
echo Close this window to stop the server (the watchdog will not restart it).
echo.

:RUN_LOOP
echo --- Run attempt !ATTEMPT!/!MAX_ATTEMPTS! ---
tune-server.exe
set EXIT_CODE=!errorlevel!

REM Auto-update hand-off: if _update_staging exists, _apply_update.bat is
REM about to swap files in. The watchdog must NOT restart tune-server.exe
REM under it — that would race the robocopy.
if exist "_update_staging" (
    echo.
    echo Update in progress, watchdog stepping aside.
    echo The applier will restart Tune Server when the swap is complete.
    goto :END
)

if !EXIT_CODE! equ 0 (
    echo.
    echo Tune Server exited cleanly. Closing watchdog.
    goto :END
)

echo.
echo ============================================================
echo  Tune Server crashed ^(exit code !EXIT_CODE!^), attempt !ATTEMPT!/!MAX_ATTEMPTS!.
echo ============================================================

if !ATTEMPT! geq !MAX_ATTEMPTS! goto :GIVE_UP

REM Linear backoff: 5s, 10s, 15s
set /a DELAY=!ATTEMPT!*5
echo Restarting in !DELAY!s ^(Ctrl+C now to abort^)...
ping -n !DELAY! 127.0.0.1 >nul
set /a ATTEMPT+=1
goto :RUN_LOOP

:GIVE_UP
echo.
echo Tune Server has crashed !MAX_ATTEMPTS! times in a row. Watchdog stopping.
if exist "tune-server.log" (
    echo Last 40 lines of tune-server.log:
    echo ------------------------------------------------------------
    powershell -NoProfile -Command "Get-Content -Path 'tune-server.log' -Tail 40 -ErrorAction SilentlyContinue"
    echo ------------------------------------------------------------
) else (
    echo No log file was written ^(server may have failed before logging init^).
)
echo.
echo Open the Web UI ^(if reachable^) and click "Telecharger le diagnostic"
echo to send a support bundle, or attach tune-server.log to your bug report.
echo.

:END
pause
endlocal

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
REM Runs in a sibling cmd so it doesn't block the server in this window.
REM ---------------------------------------------------------------------------
start "" /B cmd /c "for /L %%i in (1,1,30) do (curl -s --max-time 1 http://localhost:8888/api/v1/system/health >nul 2>&1 && (start http://localhost:8888 ^&^& exit) || ping -n 2 127.0.0.1 >nul)"

REM ---------------------------------------------------------------------------
REM Run the server. Logs are written to tune-server.log by the app itself.
REM Closing this window stops the server (Ctrl+C also works).
REM ---------------------------------------------------------------------------
echo Starting Tune Server...
echo Web UI will open at http://localhost:8888 once ready.
echo Logs are written to: %cd%\tune-server.log
echo Close this window to stop the server.
echo.

tune-server.exe
set EXIT_CODE=%errorlevel%

REM ---------------------------------------------------------------------------
REM Post-mortem if the server exits unexpectedly.
REM ---------------------------------------------------------------------------
echo.
echo ============================================================
echo  Server exited with code %EXIT_CODE%.
echo ============================================================
if exist "tune-server.log" (
    echo Last 30 lines of tune-server.log:
    echo ------------------------------------------------------------
    powershell -NoProfile -Command "Get-Content -Path 'tune-server.log' -Tail 30 -ErrorAction SilentlyContinue"
    echo ------------------------------------------------------------
) else (
    echo No log file was written ^(server may have failed before logging init^).
)
echo.
echo If this is unexpected, please share tune-server.log with support.
echo.
pause
endlocal

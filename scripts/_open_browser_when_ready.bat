@echo off
REM Polls /api/v1/system/health and opens the default browser to
REM http://localhost:8888 once the server responds. Spawned by
REM start-tune-server.bat as a background helper. Stops after 30 tries
REM (~60 seconds).

setlocal enabledelayedexpansion

set MAX_TRIES=30
for /L %%i in (1,1,%MAX_TRIES%) do (
    curl -s --max-time 1 http://localhost:8888/api/v1/system/health >nul 2>&1
    if not errorlevel 1 (
        start http://localhost:8888
        exit /b 0
    )
    ping -n 2 127.0.0.1 >nul
)

REM Server didn't come up in time. Silent — the user is already looking
REM at the main launcher window and will see whatever error it printed.
exit /b 1

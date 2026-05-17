@echo off
setlocal enabledelayedexpansion

REM ===========================================================================
REM Tune Server — One-shot updater for Windows (manual fallback)
REM ===========================================================================
REM Run this from the folder where tune-server.exe lives. Use this when the
REM in-app auto-update fails and you need a guaranteed clean install.
REM
REM Workflow:
REM   0. discover the latest GitHub release tag (fall back to a known good)
REM   1. force-stop tune-server.exe / librespot.exe (poll until really gone)
REM   2. download the windows.zip
REM   3. extract to _update_tmp\
REM   4. robocopy on top of the install dir (with /R:10 /W:3 grace)
REM   5. verify tune-server.exe got swapped (size check + copy fallback)
REM   6. relaunch
REM
REM All steps are logged to _update.log so failures are debuggable.
REM ===========================================================================

cd /d "%~dp0"
set "LOGFILE=%~dp0_update.log"
echo [%DATE% %TIME%] tune-update.bat starting > "%LOGFILE%"

REM Sanity: must be in a folder containing tune-server.exe
if not exist "tune-server.exe" (
    echo [ERROR] tune-server.exe is not in this folder.
    echo Place tune-update.bat next to your existing tune-server.exe and re-run.
    pause
    exit /b 1
)

echo [0/6] Looking up latest release...
for /f "tokens=*" %%t in ('powershell -NoProfile -Command "try { (Invoke-RestMethod -Uri 'https://api.github.com/repos/renesenses/tune-server-linux/releases/latest' -UseBasicParsing).tag_name.TrimStart('v') } catch { Write-Output '0.7.101' }"') do set "VERSION=%%t"
if "%VERSION%"=="" set "VERSION=0.7.101"
set "URL=https://github.com/renesenses/tune-server-linux/releases/download/v%VERSION%/tune-server-%VERSION%-windows.zip"

echo.
echo === Tune Server Updater ===
echo Target version: v%VERSION%
echo URL:            %URL%
echo Working dir:    %cd%
echo Log file:       %LOGFILE%
echo.
echo [%DATE% %TIME%] target=v%VERSION% >> "%LOGFILE%"

REM ---------------------------------------------------------------------------
REM 1. Stop the running server. Poll tasklist instead of relying on a fixed
REM    sleep — file handles take a moment to release after taskkill.
REM ---------------------------------------------------------------------------
echo [1/6] Stopping running tune-server.exe...
taskkill /F /IM tune-server.exe >> "%LOGFILE%" 2>&1
taskkill /F /IM librespot.exe   >> "%LOGFILE%" 2>&1
set /a TRIES=0
:wait_exit
tasklist /FI "IMAGENAME eq tune-server.exe" /FO CSV /NH 2>nul | findstr /I "tune-server.exe" >nul
if errorlevel 1 goto :proc_gone
set /a TRIES+=1
if %TRIES% GEQ 10 (
    echo [%DATE% %TIME%] tune-server.exe still alive after 20 s — proceeding anyway >> "%LOGFILE%"
    goto :proc_gone
)
ping -n 3 127.0.0.1 >nul
goto :wait_exit
:proc_gone
REM Extra grace period for OS to release the .exe file lock.
ping -n 4 127.0.0.1 >nul

REM ---------------------------------------------------------------------------
REM 2. Download the new zip.
REM ---------------------------------------------------------------------------
echo [2/6] Downloading update...
echo [%DATE% %TIME%] downloading %URL% >> "%LOGFILE%"
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%' -OutFile '_update.zip' -UseBasicParsing } catch { Write-Error $_.Exception.Message; exit 1 }" 2>> "%LOGFILE%"
if not exist "_update.zip" (
    echo [ERROR] Download failed. Check your internet connection. See _update.log
    pause
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM 3. Extract.
REM ---------------------------------------------------------------------------
echo [3/6] Extracting...
if exist "_update_tmp" rmdir /S /Q "_update_tmp"
powershell -NoProfile -Command "Expand-Archive -Path '_update.zip' -DestinationPath '_update_tmp' -Force" 2>> "%LOGFILE%"
if not exist "_update_tmp\tune-server\tune-server.exe" (
    echo [ERROR] Extracted archive doesn't have the expected layout.
    echo See _update.log for details.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM 4. Mirror new build over the install dir (with retries).
REM ---------------------------------------------------------------------------
echo [4/6] Swapping files...
echo [%DATE% %TIME%] robocopy _update_tmp\tune-server -^> . >> "%LOGFILE%"
robocopy "_update_tmp\tune-server" "." /MIR ^
    /XF .env tune_server.db tune-server.log tune-server.log.1 _update.log ^
    /XD artwork_cache backups _update_tmp _backup_* ^
    /R:10 /W:3 /NP /NJH /NJS >> "%LOGFILE%" 2>&1
set RC=%errorlevel%
echo [%DATE% %TIME%] robocopy returned %RC% >> "%LOGFILE%"
if %RC% GEQ 8 (
    echo [ERROR] robocopy failed with code %RC%. See _update.log
    pause
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM 5. Sanity-check: tune-server.exe size should match the new bundle.
REM    If sizes differ, fall back to a direct copy (robocopy can mis-handle
REM    files briefly held during its own retry loop).
REM ---------------------------------------------------------------------------
for %%I in ("_update_tmp\tune-server\tune-server.exe") do set "EXPECTED_SIZE=%%~zI"
for %%I in ("tune-server.exe") do set "LIVE_SIZE=%%~zI"
if not "%EXPECTED_SIZE%"=="%LIVE_SIZE%" (
    echo [%DATE% %TIME%] [WARN] tune-server.exe size mismatch (live=%LIVE_SIZE% expected=%EXPECTED_SIZE%) — direct copy fallback >> "%LOGFILE%"
    copy /Y "_update_tmp\tune-server\tune-server.exe" "tune-server.exe" >> "%LOGFILE%" 2>&1
)

rmdir /S /Q "_update_tmp"
del /Q "_update.zip"

REM ---------------------------------------------------------------------------
REM 6. Relaunch.
REM ---------------------------------------------------------------------------
echo [5/6] Relaunching...
echo [%DATE% %TIME%] launching start-tune-server.bat >> "%LOGFILE%"
echo.
echo Update complete. Starting tune-server v%VERSION%...
echo.
if exist "start-tune-server.bat" (
    start "" "%~dp0start-tune-server.bat"
) else (
    start "" "%~dp0tune-server.exe"
)
echo [6/6] Done.
exit /b 0

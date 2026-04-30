@echo off
setlocal enabledelayedexpansion

REM ===========================================================================
REM Tune Server — One-shot updater for Windows
REM ===========================================================================
REM Run this in the folder where tune-server.exe lives.
REM Closes the running server, downloads the latest zip, swaps files in place,
REM relaunches start-tune-server.bat.
REM ===========================================================================

cd /d "%~dp0"

REM 1. Quick sanity: must be in a folder containing tune-server.exe
if not exist "tune-server.exe" (
    echo [ERROR] tune-server.exe is not in this folder.
    echo Place tune-update.bat next to your existing tune-server.exe and re-run.
    pause
    exit /b 1
)

REM Discover the latest GitHub release tag so the script doesn't go stale
REM every patch. Fall back to a known-good version if the API is blocked.
echo [0/5] Looking up latest release...
for /f "tokens=*" %%t in ('powershell -NoProfile -Command "try { (Invoke-RestMethod -Uri 'https://api.github.com/repos/renesenses/tune-server-linux/releases/latest' -UseBasicParsing).tag_name.TrimStart('v') } catch { Write-Output '0.7.57' }"') do set "VERSION=%%t"
if "%VERSION%"=="" set "VERSION=0.7.57"
set "URL=https://github.com/renesenses/tune-server-linux/releases/download/v%VERSION%/tune-server-%VERSION%-windows.zip"

echo.
echo === Tune Server Updater ===
echo Target version: v%VERSION%
echo URL:            %URL%
echo Working dir:    %cd%
echo.

REM 2. Stop the running server (kill the process, ignore if not running)
echo [1/5] Stopping running tune-server.exe...
taskkill /F /IM tune-server.exe >nul 2>&1
taskkill /F /IM librespot.exe >nul 2>&1
timeout /t 2 /nobreak >nul

REM 3. Download the new zip
echo [2/5] Downloading update...
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%' -OutFile '_update.zip' -UseBasicParsing } catch { exit 1 }"
if not exist "_update.zip" (
    echo [ERROR] Download failed. Check your internet connection.
    pause
    exit /b 1
)

REM 4. Extract on top of the current install (overwrites tune-server.exe + _internal)
echo [3/5] Extracting...
powershell -NoProfile -Command "Expand-Archive -Path '_update.zip' -DestinationPath '_update_tmp' -Force"
if not exist "_update_tmp\tune-server" (
    echo [ERROR] Extracted archive doesn't have the expected layout.
    pause
    exit /b 1
)

echo [4/5] Swapping files...
xcopy /E /Y /Q "_update_tmp\tune-server\*" "." >nul
rmdir /S /Q "_update_tmp"
del /Q "_update.zip"

REM 5. Relaunch
echo [5/5] Relaunching...
echo.
echo Update complete. Starting tune-server v%VERSION%...
echo.
start "" "%~dp0start-tune-server.bat"
exit /b 0

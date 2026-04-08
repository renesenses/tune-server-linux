@echo off
REM Tune Server - Environment setup for Windows
REM Creates .env from .env.example if it doesn't exist.

if exist ".env" (
    echo .env already exists. Delete it first to regenerate.
    exit /b 0
)

if not exist ".env.example" (
    echo ERROR: .env.example not found.
    exit /b 1
)

copy ".env.example" ".env" >/dev/null
echo Created .env from template.
echo Edit .env to configure your Tidal, Qobuz, Deezer credentials and music directories.

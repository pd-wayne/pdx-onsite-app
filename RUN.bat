@echo off
setlocal enabledelayedexpansion

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

:: Check not running from network path
if "%APP_DIR:~0,2%"=="\\" (
    echo ERROR: Please copy the app to your local C: drive first.
    echo Example: C:\PDX\pdx_onsite_app\
    pause
    exit /b 1
)

:: Check main.py exists
if not exist "%APP_DIR%\main.py" (
    echo ERROR: main.py not found in %APP_DIR%
    echo Make sure you extracted the zip fully before running.
    pause
    exit /b 1
)

cd /d "%APP_DIR%"
python "%APP_DIR%\main.py"

if errorlevel 1 (
    echo.
    echo App exited with an error.
    echo Check pdx_onsite.log for details.
    pause
)

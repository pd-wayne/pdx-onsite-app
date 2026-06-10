@echo off
setlocal enabledelayedexpansion

:: ── Get the folder this bat file lives in ────────────────────────────────────
set "APP_DIR=%~dp0"
:: Remove trailing backslash
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

echo ===================================
echo  PDX Onsite - Setup
echo ===================================
echo.
echo App folder: %APP_DIR%
echo.

:: ── Check 1: UNC / network path ──────────────────────────────────────────────
if "%APP_DIR:~0,2%"=="\\" (
    echo ERROR: This folder is on a network drive.
    echo.
    echo Please copy the pdx_onsite_app folder to your local C: drive first.
    echo Example: C:\PDX\pdx_onsite_app\
    echo.
    echo Then run SETUP.bat from there.
    pause
    exit /b 1
)

:: ── Check 2: Path contains parentheses or was run from inside a zip ──────────
echo %APP_DIR% | findstr /C:"(" >nul 2>&1
if not errorlevel 1 (
    echo ERROR: Your folder path contains parentheses: %APP_DIR%
    echo.
    echo This usually means you downloaded the zip and it was renamed to
    echo something like "PDX_Onsite_App (1)". Windows cannot run batch files
    echo correctly from paths with parentheses.
    echo.
    echo Please:
    echo   1. Move the pdx_onsite_app folder to C:\PDX\
    echo   2. Run SETUP.bat from C:\PDX\pdx_onsite_app\
    echo.
    pause
    exit /b 1
)

:: ── Check 3: requirements.txt exists (confirms not running from inside zip) ──
if not exist "%APP_DIR%\requirements.txt" (
    echo ERROR: requirements.txt not found.
    echo.
    echo This usually means you are running SETUP.bat from inside the zip file
    echo without extracting it first.
    echo.
    echo Please:
    echo   1. Right-click the zip file
    echo   2. Click "Extract All"
    echo   3. Choose a destination like C:\PDX\
    echo   4. Open the extracted pdx_onsite_app folder
    echo   5. Double-click SETUP.bat
    echo.
    pause
    exit /b 1
)

:: ── Check 4: Python installed and on PATH ─────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo.
    echo Please install Python from https://python.org
    echo.
    echo IMPORTANT: During install, check the box that says:
    echo   "Add Python to PATH"
    echo.
    echo After installing Python, run SETUP.bat again.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo Found: %PYVER%
echo.

:: ── All checks passed — install dependencies ─────────────────────────────────
echo Installing dependencies...
echo.
cd /d "%APP_DIR%"
pip install -r "%APP_DIR%\requirements.txt"

if errorlevel 1 (
    echo.
    echo ERROR: pip install failed.
    echo.
    echo Common causes:
    echo   - No internet connection
    echo   - pip needs to be updated
    echo.
    echo Try running this command manually:
    echo   pip install flask requests Pillow python-escpos pywin32
    echo.
    pause
    exit /b 1
)

echo.
echo ===================================
echo  Setup complete!
echo ===================================
echo.
echo To start the app: double-click RUN.bat
echo.
pause

@echo off
cd /d "%~dp0"
echo ===================================
echo  PDX Onsite v2.0 - Build EXE
echo ===================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Python is required to BUILD the exe.
    echo Once built, the exe runs on any Windows machine with no Python needed.
    echo.
    echo Install Python from https://python.org
    echo IMPORTANT: check "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo Found: %PYVER%
echo.

:: Install PyInstaller and dependencies
echo Installing dependencies...
python -m pip install pyinstaller
python -m pip install -r requirements.txt
echo.

:: Clean previous build artifacts
if exist "dist\PDX_Onsite.exe" del /f "dist\PDX_Onsite.exe"
if exist "build" rmdir /s /q "build"
if exist "PDX_Onsite.spec" del /f "PDX_Onsite.spec"

:: Build the exe
echo Building exe (this takes 1-3 minutes)...
echo.

python -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name "PDX_Onsite" ^
  --add-data "src;src" ^
  --hidden-import "win32print" ^
  --hidden-import "win32api" ^
  --hidden-import "win32con" ^
  --hidden-import "pywintypes" ^
  --hidden-import "escpos.printer" ^
  --hidden-import "escpos.capabilities" ^
  --collect-data "escpos" ^
  --hidden-import "PIL._tkinter_finder" ^
  --hidden-import "PIL._imaging" ^
  --hidden-import "flask" ^
  --hidden-import "werkzeug" ^
  --hidden-import "werkzeug.serving" ^
  --hidden-import "jinja2" ^
  --hidden-import "requests" ^
  --hidden-import "sqlite3" ^
  --hidden-import "tkinter" ^
  --hidden-import "tkinter.filedialog" ^
  --hidden-import "qrcode" ^
  --hidden-import "qrcode.image.pil" ^
  --collect-all "qrcode" ^
  main.py

echo.
if exist "dist\PDX_Onsite.exe" (
    echo ===================================
    echo  Build successful!
    echo ===================================
    echo.
    echo Output: dist\PDX_Onsite.exe
    echo.
    echo This exe runs standalone on any Windows machine.
    echo No Python, no installer, no dependencies needed.
) else (
    echo ===================================
    echo  Build FAILED
    echo ===================================
    echo Check the output above for errors.
)
echo.
pause

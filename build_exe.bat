@echo off
cd /d "%~dp0"

set "PY_EXE=C:\Users\oo\miniforge3\envs\main\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

echo [1/2] Checking PyInstaller...
"%PY_EXE%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    "%PY_EXE%" -m pip install pyinstaller
)

echo [2/2] Building standalone WTranslator.exe ...
"%PY_EXE%" -m PyInstaller --noconfirm --onefile --windowed --name "WTranslator" --add-data "version.json;." --hidden-import "win32com.client" --hidden-import "pythoncom" --hidden-import "win32clipboard" main.py

echo.
if exist "dist\WTranslator.exe" (
    echo [SUCCESS] Built successfully: dist\WTranslator.exe
) else (
    echo [ERROR] Build failed.
)
pause

@echo off
cd /d "%~dp0"

set "PYTHONPATH="
set "PY_EXE=C:\Users\oo\miniforge3\envs\main\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

echo [1/3] Checking PyInstaller and generating app.ico...
"%PY_EXE%" -c "import config; config.ensure_app_ico()"
"%PY_EXE%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    "%PY_EXE%" -m pip install pyinstaller
)

if exist "dist\WTranslator.exe" del /f /q "dist\WTranslator.exe"

echo [2/3] Building fast onedir package (dist\WTranslator\WTranslator.exe)...
"%PY_EXE%" -m PyInstaller --noconfirm --onedir --windowed --name "WTranslator" --icon "app.ico" --add-data "version.json;." --add-data "app.ico;." --hidden-import "win32com.client" --hidden-import "pythoncom" --hidden-import "win32clipboard" --exclude-module numpy --exclude-module scipy --exclude-module matplotlib --exclude-module pandas --exclude-module PIL --exclude-module IPython --exclude-module jupyter --exclude-module zmq --exclude-module tornado --exclude-module openpyxl --exclude-module pypdf --exclude-module fitz --exclude-module h5py --exclude-module psutil --exclude-module pytest main.py

copy /y "version.json" "dist\WTranslator\version.json" >nul
copy /y "app.ico" "dist\WTranslator\app.ico" >nul
copy /y "install.bat" "dist\WTranslator\install.bat" >nul

echo [3/3] Creating Desktop shortcut with icon...
"%PY_EXE%" -c "import config; print(config.create_desktop_shortcut())"

echo.
if exist "dist\WTranslator\WTranslator.exe" (
    echo [SUCCESS] Built in: dist\WTranslator\WTranslator.exe
    echo [SUCCESS] Desktop shortcut created on your Desktop!
) else (
    echo [ERROR] Build failed.
)
pause

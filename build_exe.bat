@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 正在检查 PyInstaller 打包工具...
python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo 正在安装 PyInstaller...
    python -m pip install pyinstaller
)

echo [2/2] 正在打包独立桌面端 WTranslator.exe ...
python -m PyInstaller --noconfirm --onefile --windowed ^
    --name "WTranslator" ^
    --add-data "version.json;." ^
    --hidden-import "win32com.client" ^
    --hidden-import "pythoncom" ^
    --hidden-import "win32clipboard" ^
    main.py

echo.
echo ✅ 打包完成！可执行文件位于: dist\WTranslator.exe
pause

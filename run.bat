@echo off
cd /d "%~dp0"

if exist "dist\WTranslator\WTranslator.exe" (
    start "" "dist\WTranslator\WTranslator.exe"
    exit /b 0
)

set "PYW_EXE=C:\Users\oo\miniforge3\envs\main\pythonw.exe"
if not exist "%PYW_EXE%" set "PYW_EXE=pythonw"
start "" "%PYW_EXE%" main.py

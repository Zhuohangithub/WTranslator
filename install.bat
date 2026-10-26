@echo off
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\WTranslator.lnk'); ^
   $s.TargetPath='%~dp0WTranslator.exe'; ^
   $s.IconLocation='%~dp0app.ico,0'; ^
   $s.WorkingDirectory='%~dp0'; ^
   $s.Description='WTranslator'; ^
   $s.Save()"

echo [OK] Desktop shortcut created: WTranslator.lnk
echo Starting WTranslator...
start "" "%~dp0WTranslator.exe"

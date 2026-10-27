@echo off
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist "%~dp0runtime\astrbot\Scripts\python.exe" (
  echo Run the installer first.
  pause
  exit /b 1
)
"%~dp0runtime\astrbot\Scripts\python.exe" "%~dp0tools\initialize_api.py"
pause

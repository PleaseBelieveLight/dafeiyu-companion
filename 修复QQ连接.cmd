@echo off
set PYTHONUTF8=1
cd /d "%~dp0"
"%~dp0runtime\astrbot\Scripts\python.exe" "%~dp0tools\control.py" heal
pause

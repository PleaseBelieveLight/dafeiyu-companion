@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
py -3.12 --version >nul 2>&1
if errorlevel 1 (
  python "%~dp0tools\launch.py" stop %*
) else (
  py -3.12 "%~dp0tools\launch.py" stop %*
)
pause

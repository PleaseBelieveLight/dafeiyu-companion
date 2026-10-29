@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
pushd "%~dp0.." >nul
if errorlevel 1 exit /b 1

py -3.12 --version >nul 2>&1
if not errorlevel 1 goto :use_py
python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if errorlevel 1 goto :missing_python
python "%~dp0launch.py" %*
goto :finish

:use_py
py -3.12 "%~dp0launch.py" %*
goto :finish

:missing_python
echo [错误] 未找到 Python 3.12，请安装 Python 3.12 并启用 py 启动器或加入 PATH。
set "task_exit_code=1"
goto :done

:finish
set "task_exit_code=%errorlevel%"

:done
popd
if "%DAFEIYU_NO_PAUSE%"=="1" goto :return
if not "%task_exit_code%"=="0" goto :pause
if "%~1"=="configure" goto :return
if "%~1"=="open-napcat" goto :return
:pause
pause
:return
exit /b %task_exit_code%

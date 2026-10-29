@echo off
call "%~dp0tools\run.cmd" configure %*
exit /b %errorlevel%

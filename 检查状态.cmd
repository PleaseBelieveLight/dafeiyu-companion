@echo off
call "%~dp0tools\run.cmd" status %*
exit /b %errorlevel%

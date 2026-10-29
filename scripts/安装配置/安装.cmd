@echo off
call "%~dp0..\..\tools\run.cmd" install %*
exit /b %errorlevel%

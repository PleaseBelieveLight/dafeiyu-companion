@echo off
call "%~dp0..\..\tools\run.cmd" deploy %*
exit /b %errorlevel%

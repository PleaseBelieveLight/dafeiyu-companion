@echo off
call "%~dp0..\..\tools\run.cmd" open-napcat %*
exit /b %errorlevel%

@echo off
call "%~dp0..\..\tools\run.cmd" heal %*
exit /b %errorlevel%

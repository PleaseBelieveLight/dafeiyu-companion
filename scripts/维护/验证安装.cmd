@echo off
call "%~dp0..\..\tools\run.cmd" verify %*
exit /b %errorlevel%

@echo off
call "%~dp0..\..\tools\run.cmd" backup %*
exit /b %errorlevel%

@echo off
call "%~dp0..\..\tools\run.cmd" sync-content %*
exit /b %errorlevel%

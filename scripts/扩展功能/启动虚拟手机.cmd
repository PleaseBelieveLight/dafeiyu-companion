@echo off
call "%~dp0..\..\tools\run.cmd" phone-start %*
exit /b %errorlevel%

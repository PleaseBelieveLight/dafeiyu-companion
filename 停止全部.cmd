@echo off
call "%~dp0tools\run.cmd" stop %*
exit /b %errorlevel%

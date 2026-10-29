@echo off
call "%~dp0..\..\tools\run.cmd" prepare-model %*
exit /b %errorlevel%

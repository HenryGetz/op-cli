@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
"%SCRIPT_DIR%caliper.cmd" %*
exit /b %ERRORLEVEL%


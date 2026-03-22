@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "POWERSHELL_EXE=powershell"

if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
  set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
)

"%POWERSHELL_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%caliper.ps1" %*
exit /b %ERRORLEVEL%


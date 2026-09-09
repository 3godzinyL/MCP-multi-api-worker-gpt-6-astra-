@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -OpenBrowser %*
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%

@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -Mode ConfigureCodex
set "result=%errorlevel%"
pause
exit /b %result%

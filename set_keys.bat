@echo off
setlocal
cd /d "%~dp0"
call bootstrap.bat
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" manage.py set-key %*
pause

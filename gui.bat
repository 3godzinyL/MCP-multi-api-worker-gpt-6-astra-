@echo off
setlocal
echo Panel: http://127.0.0.1:4101/ui/
echo Panel otworzy sie automatycznie po uruchomieniu. Pozostaw to okno otwarte.
call "%~dp0start.bat"
exit /b %errorlevel%

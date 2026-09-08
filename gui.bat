@echo off
setlocal
echo Panel: http://127.0.0.1:4101/ui/
echo Otworz adres po komunikacie gotowosci. Pozostaw to okno otwarte.
call "%~dp0start.bat"
exit /b %errorlevel%

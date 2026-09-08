@echo off
setlocal
echo Rust dziala teraz w tym oknie. Ctrl+C zatrzymuje tylko te instancje.
echo Nie zamykaj okna podczas wykonywania zadan.
call "%~dp0start.bat"
exit /b %errorlevel%

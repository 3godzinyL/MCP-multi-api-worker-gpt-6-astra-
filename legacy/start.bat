@echo off
setlocal
cd /d "%~dp0"
call bootstrap.bat
if errorlevel 1 goto failed
".venv\Scripts\python.exe" manage.py init
if errorlevel 1 goto failed
".venv\Scripts\python.exe" manage.py check-running
if not errorlevel 1 goto already_running
:start_proxy
echo Local proxy: http://127.0.0.1:4000/v1
echo Press Ctrl+C to stop.
"%~dp0.venv\Scripts\python.exe" "%~dp0run_proxy.py"
if errorlevel 1 goto failed
exit /b 0
:already_running
echo Proxy juz dziala na http://127.0.0.1:4000/v1
echo.
echo 1. Zostaw dzialajace proxy i zamknij to okno.
echo 2. Zatrzymaj stare proxy i uruchom nowe tutaj.
echo Restart przerwie trwajace odpowiedzi API.
choice /c 12 /n /m "Wybierz [1/2]: "
if errorlevel 255 goto failed
if errorlevel 2 goto restart_proxy
exit /b 0
:restart_proxy
"%~dp0.venv\Scripts\python.exe" "%~dp0stop_proxy.py"
if errorlevel 1 goto failed
goto start_proxy
:failed
echo Proxy did not start. Read the error above.
pause
exit /b 1

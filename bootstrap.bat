@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 goto use_launcher
:use_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 goto use_path
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 goto use_venv
goto failed
:use_launcher
py -3 "%~dp0bootstrap.py"
exit /b %errorlevel%
:use_path
python "%~dp0bootstrap.py"
exit /b %errorlevel%
:use_venv
".venv\Scripts\python.exe" "%~dp0bootstrap.py"
exit /b %errorlevel%
:failed
echo Nie znaleziono dzialajacego Pythona 3.11 lub nowszego.
echo Zainstaluj Python z python.org z opcja Python Launcher, potem uruchom start.bat.
exit /b 1

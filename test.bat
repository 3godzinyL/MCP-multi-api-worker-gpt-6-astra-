@echo off
setlocal
cd /d "%~dp0"
call bootstrap.bat
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1
cargo fmt --all --check
if errorlevel 1 exit /b 1
cargo clippy --locked --all-targets -- -D warnings
if errorlevel 1 exit /b 1
cargo test --locked
if errorlevel 1 exit /b 1
cargo build --locked
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" scripts\test_rust_proxy.py
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" scripts\test_mcp.py
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" scripts\scan_repository.py
if errorlevel 1 exit /b 1
call npm ci --ignore-scripts
if errorlevel 1 exit /b 1
call npx playwright install chromium
if errorlevel 1 exit /b 1
call npm run test:ui
exit /b %errorlevel%

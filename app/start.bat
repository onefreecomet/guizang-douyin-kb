@echo off
cd /d "%~dp0"
set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=%APPDATA%\uv\python\cpython-3.13-windows-x86_64-none\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
curl -s -m 2 http://127.0.0.1:8765/api/status >nul 2>&1
if %errorlevel%==0 (
  start "" http://127.0.0.1:8765/
  exit /b
)
start "" http://127.0.0.1:8765/
"%PYEXE%" server.py --port 8765
pause

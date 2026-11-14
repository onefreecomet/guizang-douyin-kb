@echo off
chcp 65001 >nul
rem 归藏 · 手动启动（没建桌面快捷方式时用）
rem 双击本文件即可。找不到运行环境时，会提示去跑 tools\bootstrap.ps1
setlocal
cd /d "%~dp0.."

set "PY=%~dp0..\runtime\python\pythonw.exe"
if exist "%PY%" goto haveenv
set "PY=%~dp0..\.venv\Scripts\pythonw.exe"
if exist "%PY%" goto haveenv

echo.
echo 还没准备好运行环境（既没有 runtime\python 也没有 .venv）。
echo 请先运行:  powershell -ExecutionPolicy Bypass -File tools\bootstrap.ps1
echo.
pause
exit /b 1

:haveenv
rem 服务已经在跑就只把窗口叫出来，避免开第二个实例
curl -s -m 2 http://127.0.0.1:8765/api/ping >nul 2>&1
if %errorlevel%==0 (
  start "" http://127.0.0.1:8765/
  exit /b
)

start "" "%PY%" "app\launcher.py"
exit /b

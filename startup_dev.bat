@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0"

REM ============================================================
REM mini-agent development launcher (Windows CMD)
REM
REM   startup_dev.bat
REM
REM   1) backend  : http://0.0.0.0:8123  (API + SSE, all interfaces / LAN)
REM   2) frontend : http://0.0.0.0:5173  (Vite dev server, /api proxied)
REM
REM Always uses the project virtualenv interpreter (needed so that
REM MCP servers, spawned as Python child processes, share the same
REM interpreter and can complete their handshake).
REM ------------------------------------------------------------------

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [x] Virtualenv not found: %PY%
  echo.
  echo     Create one first:
  echo       scripts\setup_venv.bat
  exit /b 2
)

if not exist "web\node_modules" (
  echo ==^> installing npm dependencies ...
  pushd web
  call npm install || (popd & exit /b 1)
  popd
)

echo ==^> starting backend on http://0.0.0.0:8123
start "mini-agent backend :8123" cmd /k ".venv\Scripts\python.exe src\core\main.py --web --port 8123 --host 0.0.0.0"

echo ==^> starting frontend on http://localhost:5173
REM Vite reads VITE_API_TARGET from the environment; the child cmd inherits it
set "VITE_API_TARGET=http://127.0.0.1:8123"
pushd web
start "mini-agent frontend :5173" cmd /k "npm run dev"
popd

echo ==^> opening browser ...
REM console-independent delay (timeout fails when stdin is redirected)
ping -n 4 127.0.0.1 >nul 2>nul
start "" "http://localhost:5173"

endlocal

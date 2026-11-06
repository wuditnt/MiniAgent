@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0"

REM ============================================================
REM mini-agent production launcher (Windows CMD)
REM
REM   startup.bat            serve web/dist on http://0.0.0.0:8000  (all interfaces / LAN)
REM   startup.bat --build    force rebuild the frontend first
REM
REM Interpreter: MINI_AGENT_PYTHON in .env wins; otherwise the project
REM virtualenv. Tools (run_python / MCP / run_shell) resolve the same
REM variable, so keeping the backend on it avoids dependency drift.
REM ------------------------------------------------------------------

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0.env" (
  for /f "usebackq tokens=1,* delims==" %%A in (`findstr /B /C:"MINI_AGENT_PYTHON=" "%~dp0.env" 2^>nul`) do if not "%%~B"=="" set "PY=%%~B"
)
if not exist "%PY%" (
  echo [x] Virtualenv not found: %PY%
  echo.
  echo     Create one first:
  echo       scripts\setup_venv.bat
  exit /b 2
)

set "REBUILD="
if /i "%~1"=="--build" set "REBUILD=1"

if defined REBUILD goto build
if not exist "web\dist\index.html" goto build
goto run

:build
echo ==^> building frontend ^(web\dist^) ...
pushd web
if not exist "node_modules" (
  echo ==^> installing npm dependencies ...
  call npm install || (popd & exit /b 1)
)
call npm run build || (popd & exit /b 1)
popd

:run
echo ==^> starting backend on http://0.0.0.0:8000
start "mini-agent :8000" cmd /k ""%PY%" src\core\main.py --host 0.0.0.0"

echo ==^> opening browser ...
REM console-independent delay (timeout fails when stdin is redirected)
ping -n 3 127.0.0.1 >nul 2>nul
start "" "http://127.0.0.1:8000"

endlocal

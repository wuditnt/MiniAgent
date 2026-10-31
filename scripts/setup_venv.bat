@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0.."

REM ============================================================
REM mini-agent environment setup (Windows CMD)
REM
REM   scripts\setup_venv.bat
REM
REM   Run once after migrating / deploying the code to a new machine:
REM     create .venv  ->  install deps  ->  create .env  ->  self-test
REM
REM   Idempotent: an existing .venv is reused; only deps are synced.
REM ------------------------------------------------------------

set "ROOT=%~dp0.."
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" goto :deps

REM ---- pick a Python interpreter (prefer the py launcher) ----
set "BOOT="
where py >nul 2>nul
if not errorlevel 1 set "BOOT=py -3"
if defined BOOT goto :make
where python >nul 2>nul
if not errorlevel 1 set "BOOT=python"
if defined BOOT goto :make

echo [x] Python not found.
echo     Install Python 3.10+ (check "Add to PATH") and rerun this script.
exit /b 2

:make
echo ==^> creating virtualenv: "%VENV%"
%BOOT% -m venv "%VENV%"
if errorlevel 1 goto :fail_venv

:deps
echo ==^> upgrading pip
"%PY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo ==^> installing dependencies (requirements.txt)
"%PY%" -m pip install -r "%ROOT%\requirements.txt"
if errorlevel 1 goto :fail

echo ==^> installing this project (optional, provides the mini-agent command)
"%PY%" -m pip install -e "%ROOT%" -q
if errorlevel 1 echo [i] editable install failed; running from source still works

REM ---- .env: create from template if missing (system config only; model goes in Web UI) ----
if not exist "%ROOT%\.env" if exist "%ROOT%\.env.example" (
  copy /y "%ROOT%\.env.example" "%ROOT%\.env" >nul
  echo [i] created .env from .env.example (listen address / port)
)

echo ==^> tool-layer self-test (no API key needed)
"%PY%" -c "import sys; sys.path.insert(0, r'%ROOT%\src'); import tools; print(tools.load_report().summary())"

echo.
echo ==^> done. Next steps:
echo     1) start the backend and configure the model in Settings -^> Models (API key stays in local models.json)
echo     2) dev: startup_dev.bat   prod: startup.bat
echo     3) or run: .venv\Scripts\python.exe src\core\main.py
goto :eof

:fail_venv
echo [x] failed to create the virtualenv
exit /b 1

:fail
echo [x] dependency install failed - check network / Python version and retry
exit /b 1

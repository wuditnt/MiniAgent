@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0.."

REM ============================================================
REM mini-agent tests (Windows CMD)
REM
REM   scripts\run_tests.bat              run all free suites
REM   scripts\run_tests.bat loop         only suites matching keyword
REM   scripts\run_tests.bat --list       list suites
REM   scripts\run_tests.bat --real       include real API suites
REM   scripts\run_tests.bat --reinstall  sync deps first
REM
REM This script lives in scripts\, so it cds to the parent (project root)
REM ------------------------------------------------------------------
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [x] Virtualenv not found: %PY%
  echo.
  echo     Create one first:
  echo       py -3.13 -m venv .venv
  echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  echo     Or just run scripts\setup_venv.bat
  exit /b 2
)

if /i "%~1"=="--reinstall" (
  echo ==^> syncing dependencies
  "%PY%" -m pip install -r requirements.txt || exit /b 1
  "%PY%" -m pip install -e . -q || exit /b 1
  shift
)

if /i "%~1"=="--real" (
  echo ==^> including real API suites ^(consumes tokens^)
  set "MINI_AGENT_REAL=1"
  shift
)

echo ==^> interpreter: %PY%
"%PY%" tests\run_tests.py %*
exit /b %errorlevel%

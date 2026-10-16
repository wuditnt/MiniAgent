@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0.."

REM ============================================================
REM One-shot virtualenv setup (Windows CMD)
REM
REM   scripts\setup_venv.bat             auto-pick a Python
REM   scripts\setup_venv.bat py -3.12    specify interpreter
REM
REM This script lives in scripts\, so it cds to the parent (project root)
REM ------------------------------------------------------------------

set "BASEPY="
if not "%~1"=="" (
  where %* >nul 2>nul && set "BASEPY=%*"
)
if not defined BASEPY (
  for %%C in (py python) do (
    if not defined BASEPY (
      where %%C >nul 2>nul && set "BASEPY=%%C"
    )
  )
)
if not defined BASEPY (
  where python3 >nul 2>nul && set "BASEPY=python3"
)
if not defined BASEPY (
  echo [x] Python not found. Install Python 3.10+ or pass one explicitly:
  echo       scripts\setup_venv.bat py -3.13
  exit /b 2
)

echo ==^> base interpreter: %BASEPY%
%BASEPY% -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)"
if errorlevel 1 (
  echo [x] Need Python 3.10 or newer.
  exit /b 2
)

if exist ".venv\Scripts\python.exe" (
  echo ==^> .venv already exists, skipping creation
) else (
  echo ==^> creating .venv
  %BASEPY% -m venv .venv || exit /b 1
)

set "PY=.venv\Scripts\python.exe"

echo ==^> upgrading pip
"%PY%" -m pip install --upgrade pip -q

echo ==^> installing dependencies
"%PY%" -m pip install -r requirements.txt || exit /b 1

echo ==^> installing this project in editable mode
"%PY%" -m pip install -e . -q || exit /b 1

echo.
echo ==^> self check
"%PY%" -m core --selftest >nul && echo     tool layer OK

echo.
echo Done. Next steps:
echo   1) copy .env.example to .env and fill OPENAI_API_KEY
echo   2) activate: .venv\Scripts\activate
echo   3) tests: scripts\run_tests.bat    console: python -m core

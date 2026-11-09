@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0"

REM ============================================================
REM mini-agent one-click packaging (Windows CMD)
REM
REM   package.bat             double-click friendly (pauses at end)
REM   package.bat --no-pause  no trailing pause (for automation)
REM
REM   1) frontend  : web\dist
REM   2) resources : build\desktop-resources (app + python + ocr-pack)
REM   3) installer : desktop\release\mini-agent Setup ^<version^>.exe
REM
REM Version comes from VERSION at repo root: build_desktop.py validates it
REM and syncs desktop\package.json before electron-builder runs.
REM Every step is fail-fast.
REM ------------------------------------------------------------

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0.env" (
  for /f "usebackq tokens=1,* delims==" %%A in (`findstr /B /C:"MINI_AGENT_PYTHON=" "%~dp0.env" 2^>nul`) do if not "%%~B"=="" set "PY=%%~B"
)

echo ==^> prerequisites ...
if not exist "%PY%" (
  echo [x] Virtualenv not found: %PY%
  echo     Create one first: scripts\setup_venv.bat
  goto fail
)
where node >nul 2>nul
if errorlevel 1 (
  echo [x] Node.js not found in PATH. Install Node.js 18+ and retry.
  goto fail
)
where npm >nul 2>nul
if errorlevel 1 (
  echo [x] npm not found in PATH. Reinstall Node.js ^(npm ships with it^) and retry.
  goto fail
)

REM electron-builder rewrites release\win-unpacked; a running exe locks files.
echo ==^> closing running packaged app of this project ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root='%~dp0'; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'mini-agent.exe' -and $_.ExecutablePath -like ('*'+$root+'*') } | ForEach-Object { Write-Host ('   - stopped PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

if not exist "web\node_modules\.bin\vite.cmd" (
  echo ==^> installing web dependencies ...
  pushd web
  call npm install
  if errorlevel 1 (
    popd
    echo [x] npm install ^(web^) failed.
    goto fail
  )
  popd
)

echo ==^> building frontend ...
pushd web
call npm run build
if errorlevel 1 (
  popd
  echo [x] Frontend build failed.
  goto fail
)
popd

if not exist "desktop\node_modules\electron-builder" (
  echo ==^> installing desktop dependencies ...
  pushd desktop
  call npm install
  if errorlevel 1 (
    popd
    echo [x] npm install ^(desktop^) failed.
    goto fail
  )
  popd
)

echo ==^> packing app resources ...
"%PY%" -X utf8 scripts\build_desktop.py --skip-web
if errorlevel 1 (
  echo [x] build_desktop.py failed.
  goto fail
)

echo ==^> building installer ...
pushd desktop
call npm run dist
if errorlevel 1 (
  popd
  echo [x] electron-builder failed.
  goto fail
)
popd

set "VER="
for /f "usebackq delims=" %%V in ("VERSION") do if not defined VER set "VER=%%V"
if not defined VER (
  echo [x] Cannot read VERSION.
  goto fail
)
if not exist "desktop\release\mini-agent Setup %VER%.exe" (
  echo [x] Installer not found: desktop\release\mini-agent Setup %VER%.exe
  goto fail
)

echo.
echo ==^> done: desktop\release\mini-agent Setup %VER%.exe
powershell -NoProfile -Command "Get-Item 'desktop\release\mini-agent Setup %VER%.exe' | ForEach-Object { '    size: {0:N1} MB' -f ($_.Length/1MB) }"

if /i not "%~1"=="--no-pause" (
  echo.
  echo Press any key to close this window.
  pause >nul
)
endlocal
exit /b 0

:fail
echo.
if /i not "%~1"=="--no-pause" (
  echo Press any key to close this window.
  pause >nul
)
endlocal
exit /b 1
